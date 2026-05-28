"""
Migration tracker – durable, resumable state machine for the Ember→Angular
migration process.

State is persisted to migration_state.json after every update so the
orchestration can be safely interrupted and resumed at any time.

Migration lifecycle per artifact
─────────────────────────────────
  pending  → analyzing  → analyzed
           → migrating  → migrated
                        → validating → validated
                                     → needs_fix → migrated (retry)
                        → failed
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# State enums
# ─────────────────────────────────────────────────────────────────────────────

class ArtifactStatus(str, Enum):
    PENDING    = "pending"
    ANALYZING  = "analyzing"
    ANALYZED   = "analyzed"
    MIGRATING  = "migrating"
    MIGRATED   = "migrated"
    VALIDATING = "validating"
    VALIDATED  = "validated"
    NEEDS_FIX  = "needs_fix"
    FAILED     = "failed"
    SKIPPED    = "skipped"


class MigrationPhase(str, Enum):
    INIT        = "init"
    SCANNING    = "scanning"
    SCANNED     = "scanned"
    STRUCTURING = "structuring"
    STRUCTURED  = "structured"
    MIGRATING   = "migrating"
    DONE        = "done"
    FAILED      = "failed"


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ArtifactRecord:
    name: str
    artifact_type: str
    ember_files: List[str]          # source Ember files
    angular_files: List[str]        = field(default_factory=list)  # written Angular files
    status: ArtifactStatus          = ArtifactStatus.PENDING
    analysis_summary: str           = ""
    migration_notes: str            = ""
    validation_issues: List[str]    = field(default_factory=list)
    retry_count: int                = 0
    last_updated: str               = ""
    dependencies: List[str]         = field(default_factory=list)

    def touch(self):
        self.last_updated = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactRecord":
        d = d.copy()
        d["status"] = ArtifactStatus(d["status"])
        return cls(**d)


@dataclass
class MigrationState:
    ember_project_path: str
    angular_output_path: str
    phase: MigrationPhase               = MigrationPhase.INIT
    artifacts: Dict[str, ArtifactRecord]= field(default_factory=dict)
    module_order: List[str]             = field(default_factory=list)   # migration sequence
    current_module: Optional[str]       = None
    angular_structure_created: bool     = False
    started_at: str                     = ""
    last_saved: str                     = ""
    notes: str                          = ""

    def to_dict(self) -> dict:
        d = {
            "ember_project_path":      self.ember_project_path,
            "angular_output_path":     self.angular_output_path,
            "phase":                   self.phase.value,
            "artifacts":               {k: v.to_dict() for k, v in self.artifacts.items()},
            "module_order":            self.module_order,
            "current_module":          self.current_module,
            "angular_structure_created": self.angular_structure_created,
            "started_at":              self.started_at,
            "last_saved":              self.last_saved,
            "notes":                   self.notes,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MigrationState":
        phase     = MigrationPhase(d.get("phase", "init"))
        artifacts = {k: ArtifactRecord.from_dict(v) for k, v in d.get("artifacts", {}).items()}
        return cls(
            ember_project_path       = d["ember_project_path"],
            angular_output_path      = d["angular_output_path"],
            phase                    = phase,
            artifacts                = artifacts,
            module_order             = d.get("module_order", []),
            current_module           = d.get("current_module"),
            angular_structure_created= d.get("angular_structure_created", False),
            started_at               = d.get("started_at", ""),
            last_saved               = d.get("last_saved", ""),
            notes                    = d.get("notes", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tracker
# ─────────────────────────────────────────────────────────────────────────────

class MigrationTracker:
    """
    Durable migration state machine.  Every mutating call auto-saves to disk.
    """

    def __init__(self, state_file: str):
        self._path  = Path(state_file)
        self._state: MigrationState | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def init(self, ember_path: str, angular_path: str) -> MigrationState:
        """Start a fresh migration (or load existing state if the file exists and is non-empty)."""
        if self._path.exists() and self._path.stat().st_size > 0:
            logger.info("Resuming existing migration state from %s", self._path)
            return self.load()

        self._state = MigrationState(
            ember_project_path  = ember_path,
            angular_output_path = angular_path,
            started_at          = datetime.utcnow().isoformat(),
        )
        self._save()
        return self._state

    def load(self) -> MigrationState:
        with self._path.open(encoding="utf-8") as f:
            self._state = MigrationState.from_dict(json.load(f))
        logger.info("Loaded migration state: phase=%s, artifacts=%d",
                    self._state.phase, len(self._state.artifacts))
        return self._state

    # ── Artifact registration ──────────────────────────────────────────────────

    def register_artifacts(self, artifacts: list[dict]):
        """
        Register ember artifacts (list of dicts with name, type, files).
        Skips already-registered artifacts (idempotent).
        """
        added = 0
        for a in artifacts:
            name = a["name"]
            if name not in self._state.artifacts:
                self._state.artifacts[name] = ArtifactRecord(
                    name          = name,
                    artifact_type = a.get("type", "unknown"),
                    ember_files   = a.get("files", []),
                    dependencies  = a.get("dependencies", []),
                )
                added += 1
        if added:
            self._save()
        logger.info("Registered %d new artifacts; total=%d", added, len(self._state.artifacts))

    def set_module_order(self, order: list[str]):
        self._state.module_order = order
        self._save()

    # ── Phase transitions ──────────────────────────────────────────────────────

    def transition(self, new_phase: MigrationPhase):
        logger.info("Phase: %s → %s", self._state.phase, new_phase)
        self._state.phase = new_phase
        self._save()

    def set_current_module(self, module_name: str | None):
        self._state.current_module = module_name
        self._save()

    def mark_structure_created(self):
        self._state.angular_structure_created = True
        self._save()

    # ── Artifact status updates ────────────────────────────────────────────────

    def update_artifact(
        self,
        name: str,
        status: ArtifactStatus | None = None,
        analysis_summary: str | None = None,
        migration_notes: str | None = None,
        angular_files: list[str] | None = None,
        validation_issues: list[str] | None = None,
    ):
        rec = self._state.artifacts.get(name)
        if not rec:
            logger.warning("Artifact %s not found in tracker.", name)
            return
        if status:
            rec.status = status
        if analysis_summary is not None:
            rec.analysis_summary = analysis_summary
        if migration_notes is not None:
            rec.migration_notes = migration_notes
        if angular_files is not None:
            rec.angular_files = angular_files
        if validation_issues is not None:
            rec.validation_issues = validation_issues
        rec.touch()
        self._save()

    def increment_retry(self, name: str):
        rec = self._state.artifacts.get(name)
        if rec:
            rec.retry_count += 1
            rec.touch()
            self._save()

    # ── Queries ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> MigrationState:
        return self._state

    def pending_artifacts(self, artifact_type: str | None = None) -> list[ArtifactRecord]:
        records = [
            r for r in self._state.artifacts.values()
            if r.status in (ArtifactStatus.PENDING, ArtifactStatus.NEEDS_FIX)
        ]
        if artifact_type:
            records = [r for r in records if r.artifact_type == artifact_type]
        return records

    def completed_artifacts(self) -> list[ArtifactRecord]:
        return [
            r for r in self._state.artifacts.values()
            if r.status == ArtifactStatus.VALIDATED
        ]

    def failed_artifacts(self) -> list[ArtifactRecord]:
        return [
            r for r in self._state.artifacts.values()
            if r.status == ArtifactStatus.FAILED
        ]

    def progress_summary(self) -> str:
        total   = len(self._state.artifacts)
        done    = len(self.completed_artifacts())
        failed  = len(self.failed_artifacts())
        pending = len(self.pending_artifacts())
        in_prog = total - done - failed - pending
        lines = [
            f"Phase       : {self._state.phase.value}",
            f"Total       : {total}",
            f"Validated   : {done}",
            f"In progress : {in_prog}",
            f"Pending     : {pending}",
            f"Failed      : {failed}",
            f"Module order: {', '.join(self._state.module_order[:5])}{'…' if len(self._state.module_order) > 5 else ''}",
        ]
        return "\n".join(lines)

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save(self):
        if not self._state:
            return
        self._state.last_saved = datetime.utcnow().isoformat()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._state.to_dict(), f, indent=2)
        tmp.replace(self._path)
        logger.debug("State saved to %s", self._path)
