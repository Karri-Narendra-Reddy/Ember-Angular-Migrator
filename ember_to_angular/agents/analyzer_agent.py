"""
Analyzer Agent
──────────────
Deep-reads every file in the Ember project (handling 20 000+ lines),
understands the business purpose of each module, identifies dependencies,
and produces a structured analysis that guides migration planning.

Model: gpt-4.1  (strong code understanding, large context)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from ember_to_angular.agents.base_agent import BaseAgent
from ember_to_angular.config.settings import MODEL_ANALYZER, MAX_TOKENS_ANALYZER
from ember_to_angular.memory.vector_store import CodeVectorStore
from ember_to_angular.tools.ember_parser import EmberProject, EmberArtifact
from ember_to_angular.tools.file_reader import LargeFileReader, ScanResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert Ember.js → Angular migration analyst with deep knowledge of:
  • Ember.js 3.x / 4.x patterns (Octane, classic, glimmer components)
  • Angular 17+ standalone architecture
  • TypeScript migration from JavaScript
  • Ember Data → Angular HttpClient / NgRx Data / NGRX Store

Your job is to analyse Ember source code and produce structured JSON that will
guide the migration agents.  Be precise, thorough, and preserve ALL business
context.  NEVER summarise away important logic details.

When reading chunked files:
  • Track state across chunks (imports defined in chunk 1 may be used in chunk 5)
  • Note line numbers for every important construct
  • Identify all business rules embedded in computed properties, observers, actions
  • Flag deprecated Ember patterns that need special Angular equivalents
"""


class AnalyzerAgent(BaseAgent):
    name       = "AnalyzerAgent"
    deployment = MODEL_ANALYZER
    system_prompt = SYSTEM_PROMPT
    max_tokens = MAX_TOKENS_ANALYZER

    def __init__(self, vector_store: CodeVectorStore, reader: LargeFileReader):
        super().__init__()
        self._vs     = vector_store
        self._reader = reader

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_project(self, project: EmberProject) -> dict:
        """
        High-level project analysis.
        Returns a dict with module priorities and overall migration strategy.
        """
        summary = project.summary()
        route_tree = json.dumps(project.route_tree, indent=2)

        prompt = f"""\
Analyze this Ember project structure and produce a migration strategy.

## Project Summary
{summary}

## Route Tree
{route_tree}

## Artifact Types
{self._artifact_type_summary(project)}

Return JSON with this exact structure:
{{
  "project_overview": "...",
  "business_domains": ["domain1", "domain2"],
  "angular_architecture_recommendation": "...",
  "migration_order": ["module1", "module2", ...],
  "shared_dependencies": ["service1", "model1", ...],
  "risks": ["risk1", "risk2"],
  "estimated_complexity": "low|medium|high"
}}
"""
        result = self.call_json(prompt, fresh=True)
        if not result:
            result = {
                "project_overview": "Analysis failed",
                "migration_order": list(project.route_tree.keys()),
                "shared_dependencies": [],
                "risks": ["LLM analysis failed – using static analysis fallback"],
                "estimated_complexity": "high",
            }
        logger.info("Project analysis complete. Complexity: %s", result.get("estimated_complexity"))
        return result

    def analyze_artifact(self, artifact: EmberArtifact) -> dict:
        """
        Deep-analyze a single Ember artifact.
        Reads ALL chunks of its file(s) and builds a complete picture.
        """
        # Index the file into the vector store for later retrieval
        scan = self._reader.read(artifact.file_path)
        self._vs.index_scan_results([scan], prefix=f"{artifact.name}::")

        # If the file has template, index that too
        if artifact.template_path:
            tmpl_scan = self._reader.read(artifact.template_path)
            self._vs.index_scan_results([tmpl_scan], prefix=f"{artifact.name}::tmpl::")

        # Build per-chunk analysis for very large files
        chunk_analyses: list[str] = []
        self.reset_conversation()

        for chunk in scan.chunks:
            chunk_prompt = f"""\
Analyzing {artifact.artifact_type} "{artifact.name}".
{chunk.header()}

```javascript
{chunk.content[:8000]}
```

In 3-5 sentences summarise the business logic in this chunk.
List any: actions, computed properties, service calls, API calls, Ember-specific patterns.
Focus on WHAT it does (business), not HOW it's implemented.
"""
            analysis = self.call(chunk_prompt)
            chunk_analyses.append(f"[Chunk {chunk.chunk_index + 1}]\n{analysis}")
            logger.debug("Analyzed chunk %d/%d of %s", chunk.chunk_index + 1, scan.chunk_count, artifact.name)

        # Synthesize all chunks into a final structured analysis
        synthesis_prompt = f"""\
You have analyzed all {scan.chunk_count} chunks of the Ember {artifact.artifact_type} "{artifact.name}".

Chunk-by-chunk findings:
{chr(10).join(chunk_analyses)}

Known metadata:
  - Service injections: {artifact.service_injections}
  - Actions: {artifact.actions}
  - Computed properties: {artifact.computed_props}
  - Lifecycle hooks: {artifact.lifecycle_hooks}
  - Template components used: {artifact.template_components}
  - Dependencies: {artifact.imports[:20]}

Produce a JSON migration blueprint:
{{
  "name": "{artifact.name}",
  "type": "{artifact.artifact_type}",
  "business_purpose": "...",
  "business_rules": ["rule1", "rule2"],
  "angular_equivalent": "Component|Service|Pipe|Directive|Resolver|Guard|...",
  "angular_name": "...",
  "required_angular_services": ["..."],
  "required_angular_models": ["..."],
  "lifecycle_mapping": {{
    "ember_hook": "angular_lifecycle_equivalent"
  }},
  "action_mapping": {{
    "ember_action": "angular_method_or_event"
  }},
  "computed_mapping": {{
    "computed_prop": "observable_or_signal_equivalent"
  }},
  "template_notes": "...",
  "migration_complexity": "low|medium|high",
  "special_considerations": ["..."],
  "files_to_create": ["path1", "path2"]
}}
"""
        blueprint = self.call_json(synthesis_prompt)
        if not blueprint:
            blueprint = {
                "name": artifact.name,
                "type": artifact.artifact_type,
                "business_purpose": "Unknown – LLM synthesis failed",
                "migration_complexity": "high",
                "files_to_create": [],
            }

        logger.info("Blueprint ready for %s (%s)", artifact.name, blueprint.get("migration_complexity"))
        return blueprint

    def analyze_module(self, module_name: str, artifacts: list[EmberArtifact]) -> dict:
        """
        Analyze an entire logical module (a group of related artifacts).
        Understands inter-artifact relationships and produces a module plan.
        """
        artifact_summaries = "\n\n".join(a.summary() for a in artifacts[:20])

        prompt = f"""\
Analyze the Ember module "{module_name}" which contains {len(artifacts)} artifacts.

## Artifacts
{artifact_summaries}

## Cross-Artifact Dependencies
{self._dependency_map(artifacts)}

Produce a JSON module migration plan:
{{
  "module_name": "{module_name}",
  "angular_module_name": "...",
  "angular_feature_path": "src/app/...",
  "business_purpose": "...",
  "migration_order_within_module": ["artifact1", "artifact2"],
  "shared_state_approach": "NgRx|BehaviorSubject|Signal|Service",
  "routing_structure": {{
    "base_path": "...",
    "child_routes": []
  }},
  "estimated_files": 0,
  "complexity": "low|medium|high"
}}
"""
        result = self.call_json(prompt, fresh=True)
        return result or {"module_name": module_name, "angular_feature_path": f"src/app/{module_name}"}

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _artifact_type_summary(project: EmberProject) -> str:
        counts: dict[str, int] = {}
        for a in project.artifacts:
            counts[a.artifact_type] = counts.get(a.artifact_type, 0) + 1
        return "\n".join(f"  {t}: {c}" for t, c in sorted(counts.items()))

    @staticmethod
    def _dependency_map(artifacts: list[EmberArtifact]) -> str:
        lines = []
        for a in artifacts:
            if a.service_injections:
                lines.append(f"  {a.name} depends on services: {a.service_injections}")
            if a.template_components:
                lines.append(f"  {a.name} uses components: {a.template_components[:5]}")
        return "\n".join(lines) or "  (no cross-dependencies detected)"
