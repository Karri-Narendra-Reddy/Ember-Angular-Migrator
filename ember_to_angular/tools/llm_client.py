"""
LLM Client – low-level APIM / Azure OpenAI helpers.

Extracted here (rather than in base_agent.py) to avoid circular imports
between the memory layer and the agents layer.
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any

import requests

from ember_to_angular.config.settings import (
    APIM_BASE_URL,
    APIM_KEY,
    APIM_VERSION,
    O4_MINI_VERSION,
    ENDPOINT,
)

logger = logging.getLogger(__name__)


def http_chat(
    messages: list[dict],
    deployment: str,
    max_tokens: int = 4000,
    api_version: str = APIM_VERSION,
    retries: int = 3,
) -> str | None:
    """POST to Azure APIM chat-completions endpoint with retry logic."""
    url = (
        f"{APIM_BASE_URL}/{ENDPOINT}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )
    headers = {"api-key": APIM_KEY, "Content-Type": "application/json"}
    payload: dict[str, Any] = {"messages": messages}

    if deployment.startswith("o4"):
        payload["max_completion_tokens"] = max_tokens
    else:
        payload["max_tokens"] = max_tokens

    for attempt in range(retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            elif resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("Rate-limited; retrying in %ss …", wait)
                time.sleep(wait)
            else:
                logger.error("APIM error %s: %s", resp.status_code, resp.text)
                return None
        except Exception:
            logger.error("HTTP call failed (attempt %d):\n%s", attempt + 1, traceback.format_exc())
            time.sleep(2 ** attempt)
    return None


def langchain_chat(
    messages: list[dict],
    deployment: str,
    max_tokens: int = 4000,
    api_version: str = APIM_VERSION,
) -> str | None:
    """Call APIM via LangChain AzureChatOpenAI."""
    try:
        import logging as _logging
        _logging.getLogger("httpx").setLevel(_logging.WARNING)

        from langchain_openai import AzureChatOpenAI
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

        llm = AzureChatOpenAI(
            api_version      = api_version,
            api_key          = APIM_KEY,
            azure_endpoint   = f"{APIM_BASE_URL}/{ENDPOINT}",
            azure_deployment = deployment,
            max_tokens       = max_tokens,
        )
        lc_messages = []
        for m in messages:
            role, content = m["role"], m["content"]
            if role == "system":
                lc_messages.append(SystemMessage(content=content))
            elif role == "user":
                lc_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_messages.append(AIMessage(content=content))
        response = llm.invoke(lc_messages)
        return response.content
    except Exception:
        logger.error("LangChain call failed:\n%s", traceback.format_exc())
        return None


def get_embeddings(texts: list[str], deployment: str = "text-embedding-3-large") -> list[list[float]] | None:
    """Fetch embeddings via APIM."""
    url = (
        f"{APIM_BASE_URL}/{ENDPOINT}/openai/deployments/{deployment}"
        f"/embeddings?api-version={APIM_VERSION}"
    )
    headers = {"api-key": APIM_KEY, "Content-Type": "application/json"}
    payload = {"input": texts, "input_type": "query"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code == 200:
            return [item["embedding"] for item in resp.json()["data"]]
        else:
            logger.error("Embedding error %s: %s", resp.status_code, resp.text)
            return None
    except Exception:
        logger.error("Embedding call failed:\n%s", traceback.format_exc())
        return None
