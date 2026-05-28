"""
Base agent class – wraps the Azure APIM / OpenAI connection layer documented
in knowledge.py.  Every specialist agent inherits from this.

LLM functions live in tools/llm_client.py to avoid circular imports
between the memory layer and the agents layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ember_to_angular.config.settings import APIM_VERSION, O4_MINI_VERSION
from ember_to_angular.tools.llm_client import (
    http_chat as _http_chat,
    langchain_chat as _langchain_chat,
    get_embeddings,   # re-exported so callers can do: from base_agent import get_embeddings
)

logger = logging.getLogger(__name__)


class BaseAgent:
    """
    Foundation for all migration agents.

    Subclasses set:
        name            – human-readable agent name
        deployment      – Azure OpenAI deployment to use
        system_prompt   – default system message
        max_tokens      – token budget for responses
        use_reasoning   – True for o4-mini (reasoning model)
    """

    name: str          = "BaseAgent"
    deployment: str    = "gpt-4.1"
    system_prompt: str = "You are a helpful AI assistant."
    max_tokens: int    = 4000
    use_reasoning: bool= False

    def __init__(self, extra_context: str = ""):
        self._conversation: list[dict] = []
        self._extra_context = extra_context
        self._call_count = 0

    # ── Core call ──────────────────────────────────────────────────────────────

    def call(self, user_message: str, *, fresh: bool = False) -> str:
        """
        Send a message to the LLM.

        Args:
            user_message: The prompt for this turn.
            fresh:        If True, clears conversation history before calling.

        Returns:
            LLM response as plain string.
        """
        if fresh:
            self._conversation = []

        api_version = O4_MINI_VERSION if self.use_reasoning else APIM_VERSION

        messages: list[dict] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        messages.extend(self._conversation)
        messages.append({"role": "user", "content": user_message})

        self._call_count += 1
        logger.info("[%s] LLM call #%d (deployment=%s)", self.name, self._call_count, self.deployment)

        response = _http_chat(
            messages,
            deployment  = self.deployment,
            max_tokens  = self.max_tokens,
            api_version = api_version,
        )
        if response is None:
            response = _langchain_chat(
                messages,
                deployment  = self.deployment,
                max_tokens  = self.max_tokens,
                api_version = api_version,
            )

        if response is None:
            response = "[ERROR] LLM returned no response."

        self._conversation.append({"role": "user",      "content": user_message})
        self._conversation.append({"role": "assistant", "content": response})

        return response

    # ── Structured output ──────────────────────────────────────────────────────

    def call_json(self, user_message: str, *, fresh: bool = False) -> dict | list | None:
        """Call LLM and parse the response as JSON."""
        raw     = self.call(user_message, fresh=fresh)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines   = cleaned.splitlines()
            cleaned = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    pass
            logger.error("[%s] Failed to parse JSON from response:\n%s", self.name, raw[:500])
            return None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        base = self.system_prompt
        if self._extra_context:
            base += f"\n\n## Additional Context\n{self._extra_context}"
        return base

    def reset_conversation(self):
        """Clear the rolling conversation history."""
        self._conversation = []

    def inject_context(self, context_block: str):
        """Inject retrieved context as an assistant turn."""
        self._conversation.append({
            "role":    "assistant",
            "content": f"[Retrieved context for this task]\n{context_block}",
        })

    @property
    def call_count(self) -> int:
        return self._call_count
