"""
Validator Agent
───────────────
Reviews migrated Angular code for:
  1. Business logic completeness (nothing dropped from Ember original)
  2. Angular syntax correctness
  3. TypeScript strict-mode compliance
  4. Missing imports / broken references
  5. Template binding correctness

Model: gpt-4.1-mini (efficient – validation is narrower than generation)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List

from ember_to_angular.agents.base_agent import BaseAgent
from ember_to_angular.config.settings import MODEL_VALIDATOR, MAX_TOKENS_VALIDATOR
from ember_to_angular.tools.ember_parser import EmberArtifact
from ember_to_angular.tools.file_reader import LargeFileReader

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert Angular 17 code reviewer and TypeScript specialist.

Your job is to compare migrated Angular code against its original Ember source
and verify:
  1. No business logic was lost or altered
  2. Angular APIs are used correctly (standalone, inject(), signals, etc.)
  3. TypeScript types are correct and strict
  4. Template bindings are syntactically valid
  5. All imports are present and correct
  6. Lifecycle hooks map correctly
  7. Observables are properly subscribed/unsubscribed (no memory leaks)

Return structured JSON with issue severity: error | warning | info.
"""


@dataclass
class ValidationIssue:
    severity: str       # error | warning | info
    file: str
    line_hint: str
    description: str
    suggestion: str

    def to_dict(self) -> dict:
        return {
            "severity":    self.severity,
            "file":        self.file,
            "line_hint":   self.line_hint,
            "description": self.description,
            "suggestion":  self.suggestion,
        }


@dataclass
class ValidationResult:
    artifact_name: str
    passed: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    score: int = 100   # 0–100 quality score
    summary: str = ""

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def to_dict(self) -> dict:
        return {
            "artifact_name": self.artifact_name,
            "passed":        self.passed,
            "score":         self.score,
            "summary":       self.summary,
            "issues":        [i.to_dict() for i in self.issues],
        }


class ValidatorAgent(BaseAgent):
    name          = "ValidatorAgent"
    deployment    = MODEL_VALIDATOR
    system_prompt = SYSTEM_PROMPT
    max_tokens    = MAX_TOKENS_VALIDATOR

    def __init__(self, reader: LargeFileReader):
        super().__init__()
        self._reader = reader

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def validate(
        self,
        artifact: EmberArtifact,
        migrated_files: dict[str, str],
        blueprint: dict,
    ) -> ValidationResult:
        """
        Validate migrated Angular files against the original Ember artifact.
        """
        self.reset_conversation()
        logger.info("Validating %s '%s'", artifact.artifact_type, artifact.name)

        ember_source = self._read_ember_source(artifact)
        angular_code = self._format_migrated_files(migrated_files)

        prompt = f"""\
Validate the Angular migration of Ember {artifact.artifact_type} "{artifact.name}".

## Original Ember Source
```javascript
{ember_source[:6000]}
```

## Migration Blueprint
{json.dumps(blueprint, indent=2)}

## Migrated Angular Files
{angular_code[:10000]}

Check:
1. Are all business rules from the Ember source present in Angular?
2. Are there any Angular syntax errors or anti-patterns?
3. Are all imports correct?
4. Are computed properties correctly migrated (signals/getters/observables)?
5. Are lifecycle hooks correctly mapped?
6. Are all actions/methods present?
7. Is TypeScript strict mode compliant?

Return JSON:
{{
  "passed": true|false,
  "score": 0-100,
  "summary": "...",
  "issues": [
    {{
      "severity": "error|warning|info",
      "file": "filename.ts",
      "line_hint": "near line X or function Y",
      "description": "...",
      "suggestion": "..."
    }}
  ]
}}
"""
        result_dict = self.call_json(prompt, fresh=True)

        if not result_dict:
            return ValidationResult(
                artifact_name = artifact.name,
                passed        = False,
                score         = 0,
                summary       = "Validation LLM call failed",
                issues        = [ValidationIssue(
                    severity    = "error",
                    file        = artifact.file_path,
                    line_hint   = "N/A",
                    description = "Validator could not process this artifact",
                    suggestion  = "Manual review required",
                )],
            )

        issues = [
            ValidationIssue(
                severity    = i.get("severity", "warning"),
                file        = i.get("file", "unknown"),
                line_hint   = i.get("line_hint", ""),
                description = i.get("description", ""),
                suggestion  = i.get("suggestion", ""),
            )
            for i in result_dict.get("issues", [])
        ]

        result = ValidationResult(
            artifact_name = artifact.name,
            passed        = result_dict.get("passed", False) and not any(i.severity == "error" for i in issues),
            score         = result_dict.get("score", 50),
            summary       = result_dict.get("summary", ""),
            issues        = issues,
        )

        logger.info(
            "Validation result for %s: passed=%s score=%d errors=%d warnings=%d",
            artifact.name, result.passed, result.score, result.error_count, result.warning_count,
        )
        return result

    def fix_issues(
        self,
        artifact: EmberArtifact,
        migrated_files: dict[str, str],
        validation_result: ValidationResult,
    ) -> dict[str, str]:
        """
        Ask the LLM to fix validation errors.
        Returns updated file content for files with errors.
        """
        if not validation_result.has_errors:
            return migrated_files

        error_descriptions = "\n".join(
            f"- [{i.severity}] {i.file}: {i.description}\n  Fix: {i.suggestion}"
            for i in validation_result.issues
            if i.severity == "error"
        )

        angular_code = self._format_migrated_files(migrated_files)

        prompt = f"""\
Fix the following errors in the Angular migration of "{artifact.name}".

## Errors to Fix
{error_descriptions}

## Current Angular Code
{angular_code[:10000]}

Return JSON with the fixed file contents:
{{
  "relative/path/to/file.ts": "full corrected TypeScript content",
  ...
}}
Only include files that were changed.
"""
        fixed = self.call_json(prompt, fresh=True)
        if fixed and isinstance(fixed, dict):
            merged = dict(migrated_files)
            merged.update(fixed)
            logger.info("Applied %d fixes to %s", len(fixed), artifact.name)
            return merged

        logger.warning("Auto-fix failed for %s; returning original files.", artifact.name)
        return migrated_files

    def validate_project_structure(self, angular_files: list[str]) -> dict:
        """
        High-level check that the Angular project structure is coherent.
        """
        file_list = "\n".join(sorted(angular_files))

        prompt = f"""\
Review the Angular project file structure:

{file_list}

Check:
1. Are all routes accounted for?
2. Are there orphaned components (no route)?
3. Are all services importable?
4. Is the barrel index (index.ts) pattern followed?
5. Are models colocated with their services?

Return JSON:
{{
  "coherent": true|false,
  "issues": ["..."],
  "suggestions": ["..."]
}}
"""
        result = self.call_json(prompt, fresh=True)
        return result or {"coherent": False, "issues": ["Structure validation failed"]}

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _read_ember_source(self, artifact: EmberArtifact) -> str:
        scan = self._reader.read(artifact.file_path)
        if scan.error:
            return f"// Could not read {artifact.file_path}: {scan.error}"
        return scan.full_content()[:6000]

    @staticmethod
    def _format_migrated_files(files: dict[str, str]) -> str:
        parts = []
        for path, content in files.items():
            parts.append(f"=== {path} ===\n{content}")
        return "\n\n".join(parts)
