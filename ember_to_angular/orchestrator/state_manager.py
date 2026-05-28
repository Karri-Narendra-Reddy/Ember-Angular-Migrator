"""
State Manager
─────────────
Provides a clean façade over MigrationTracker with additional reporting
and introspection capabilities.  Used by the CLI and monitoring tools.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ember_to_angular.memory.migration_tracker import (
    MigrationTracker, MigrationState, ArtifactRecord, ArtifactStatus, MigrationPhase
)

logger = logging.getLogger(__name__)


class StateManager:
    """
    High-level interface for inspecting and manipulating migration state.
    Wraps MigrationTracker with convenience methods.
    """

    def __init__(self, state_file: str):
        self._tracker = MigrationTracker(state_file)
        self._state_file = state_file

    def load(self) -> MigrationState | None:
        path = Path(self._state_file)
        if not path.exists():
            return None
        return self._tracker.load()

    def exists(self) -> bool:
        return Path(self._state_file).exists()

    # ── Reports ────────────────────────────────────────────────────────────────

    def progress_report(self) -> dict:
        if not self.exists():
            return {"status": "not_started"}

        state = self._tracker.load()
        by_status: dict[str, list[str]] = {}
        for name, rec in state.artifacts.items():
            s = rec.status.value
            by_status.setdefault(s, []).append(name)

        return {
            "phase":           state.phase.value,
            "total":           len(state.artifacts),
            "by_status":       {k: len(v) for k, v in by_status.items()},
            "current_module":  state.current_module,
            "started_at":      state.started_at,
            "last_saved":      state.last_saved,
        }

    def detailed_report(self) -> str:
        report = self.progress_report()
        lines  = ["=== Migration Progress ==="]
        for k, v in report.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def failed_artifacts(self) -> list[str]:
        if not self.exists():
            return []
        state = self._tracker.load()
        return [
            name for name, rec in state.artifacts.items()
            if rec.status == ArtifactStatus.FAILED
        ]

    def reset_artifact(self, name: str):
        """Reset a failed/stuck artifact back to PENDING for retry."""
        state = self._tracker.load()
        rec = state.artifacts.get(name)
        if rec:
            self._tracker.update_artifact(name, status=ArtifactStatus.PENDING)
            logger.info("Reset artifact %s → PENDING", name)
        else:
            logger.warning("Artifact %s not found in state.", name)

    def reset_all_failed(self):
        """Reset all FAILED artifacts to PENDING."""
        failed = self.failed_artifacts()
        for name in failed:
            self.reset_artifact(name)
        logger.info("Reset %d failed artifacts to PENDING", len(failed))
        return failed

    def export_report(self, output_path: str):
        """Export a full JSON report to a file."""
        if not self.exists():
            logger.warning("No state file found at %s", self._state_file)
            return
        state = self._tracker.load()
        report = {
            "summary":   self.progress_report(),
            "artifacts": {name: rec.to_dict() for name, rec in state.artifacts.items()},
        }
        Path(output_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Report exported to %s", output_path)
