"""
Orchestrator
────────────
Thin wrapper that wires all agents, infrastructure, and the LangGraph
StateGraph together, then kicks off the migration with a single .run() call.

The actual workflow logic lives in migration_graph.py (the LangGraph graph).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ember_to_angular.agents.analyzer_agent import AnalyzerAgent
from ember_to_angular.agents.migration_agent import MigrationAgent
from ember_to_angular.agents.structure_agent import StructureAgent
from ember_to_angular.agents.validator_agent import ValidatorAgent
from ember_to_angular.config.settings import (
    ANGULAR_OUTPUT_PATH,
    STATE_FILE,
    VECTOR_STORE_PATH,
)
from ember_to_angular.memory.migration_tracker import MigrationPhase, MigrationTracker
from ember_to_angular.memory.vector_store import CodeVectorStore
from ember_to_angular.orchestrator.migration_graph import (
    MigrationGraphState,
    build_migration_graph,
)
from ember_to_angular.tools.ember_parser import EmberParser
from ember_to_angular.tools.file_reader import LargeFileReader
from ember_to_angular.tools.file_writer import FileWriter

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """
    Entry-point for the Ember → Angular migration pipeline.

    Wires all specialist agents and infrastructure, compiles the LangGraph
    StateGraph, then invokes it with the initial state.

    Parameters
    ----------
    ember_path         : root of the Ember project to migrate
    angular_path       : destination for the Angular output
    state_file         : JSON file used to persist migration state across runs
    vector_store_path  : directory for the embedding vector store
    dry_run            : if True, analyse but do not write any files
    """

    def __init__(
        self,
        ember_path:        str,
        angular_path:      str | None = None,
        state_file:        str | None = None,
        vector_store_path: str | None = None,
        dry_run:           bool = False,
    ):
        self._ember_path   = ember_path
        self._angular_path = angular_path or ANGULAR_OUTPUT_PATH
        self._dry_run      = dry_run

        # ── Infrastructure ────────────────────────────────────────────────────
        self._reader  = LargeFileReader()
        self._tracker = MigrationTracker(state_file or STATE_FILE)
        self._vs      = CodeVectorStore(vector_store_path or VECTOR_STORE_PATH)
        self._writer  = FileWriter(
            output_root   = self._angular_path,
            dry_run       = dry_run,
            manifest_path = str(Path(self._angular_path) / "migration_manifest.json"),
        )

        # ── Specialist agents ─────────────────────────────────────────────────
        self._analyzer  = AnalyzerAgent(self._vs, self._reader)
        self._structure = StructureAgent(self._angular_path, self._writer)
        self._migrator  = MigrationAgent(self._vs, self._reader, self._writer, self._angular_path)
        self._validator = ValidatorAgent(self._reader)
        self._parser    = EmberParser(self._reader)

        # ── LangGraph (compiled once, reused across resume calls) ─────────────
        self._graph = build_migration_graph(
            analyzer  = self._analyzer,
            structure = self._structure,
            migrator  = self._migrator,
            validator = self._validator,
            parser    = self._parser,
            reader    = self._reader,
            vs        = self._vs,
            tracker   = self._tracker,
            writer    = self._writer,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Execute (or resume) the full migration pipeline.

        The LangGraph drives the workflow; this method:
          1. Initialises (or loads) migration state
          2. Builds the initial graph input
          3. Invokes the compiled graph
          4. Returns the final report dict

        Returns
        -------
        dict with keys: phase, validated, failed, failed_artifacts, …
        """
        logger.info("=" * 60)
        logger.info("Ember → Angular | LangGraph Orchestrator")
        logger.info("Source  : %s", self._ember_path)
        logger.info("Output  : %s", self._angular_path)
        logger.info("Dry-run : %s", self._dry_run)
        logger.info("=" * 60)

        # Initialise or resume tracker
        tracker_state = self._tracker.init(self._ember_path, self._angular_path)

        # Build initial LangGraph state
        initial_state: MigrationGraphState = {
            # Required inputs
            "ember_path":   self._ember_path,
            "angular_path": self._angular_path,

            # Populated by scan_node
            "artifact_registry": {},
            "route_tree":        {},
            "module_map":        {},

            # Populated by downstream nodes
            "project_analysis": {},
            "structure_plan":   {},
            "module_order":     [],
            "module_queue":     [],
            "artifact_queue":   [],

            # Current context (updated per step)
            "current_module":        "",
            "current_artifact_name": "",
            "artifact_blueprint":    {},
            "retry_count":           0,

            # Accumulated (reducers handle merging)
            "migrated_files":  {},
            "validated_names": [],
            "failed_names":    [],
            "errors":          [],

            # Validation pass
            "validation_passed": False,
            "validation_issues": [],

            # Final report (populated by finalize_node)
            "report": {},
        }

        # ── Resume support ────────────────────────────────────────────────────
        # If the tracker already has registered artifacts and a module order,
        # rebuild state so the graph picks up where it left off.
        if tracker_state.artifacts and tracker_state.module_order:
            logger.info("Resuming from saved tracker state (phase=%s)", tracker_state.phase)
            initial_state = self._resume_state(initial_state, tracker_state)

        try:
            final_state: MigrationGraphState = self._graph.invoke(
                initial_state,
                config={"recursion_limit": 10_000},
            )
            report = final_state.get("report", {})
        except KeyboardInterrupt:
            logger.warning("Interrupted – tracker state saved; re-run to resume.")
            report = self._partial_report()
        except Exception as e:
            logger.error("Graph execution error: %s", e, exc_info=True)
            self._tracker.transition(MigrationPhase.FAILED)
            report = self._partial_report()

        self._print_report(report)
        return report

    def status(self) -> str:
        """Return current migration progress (callable externally)."""
        return self._tracker.progress_summary()

    # ─────────────────────────────────────────────────────────────────────────
    # Resume helper
    # ─────────────────────────────────────────────────────────────────────────

    def _resume_state(
        self,
        initial_state: MigrationGraphState,
        tracker_state,
    ) -> MigrationGraphState:
        """
        Re-parse the Ember project to reconstruct the registry/module maps,
        then skip already-validated artifacts from the queues.
        """
        try:
            project = self._parser.parse(self._ember_path)
        except Exception:
            logger.warning("Could not re-parse Ember project for resume; starting fresh.")
            return initial_state

        registry: dict = {}
        for a in project.artifacts:
            registry[a.name] = {
                "type": a.artifact_type, "file_path": a.file_path,
                "template_path": a.template_path,
                "imports": a.imports, "service_injections": a.service_injections,
                "actions": a.actions, "computed_props": a.computed_props,
                "lifecycle_hooks": a.lifecycle_hooks,
                "model_attrs": a.model_attrs, "template_components": a.template_components,
            }

        module_map = {
            mod: [a.name for a in arts]
            for mod, arts in project.modules.items()
        }

        # Determine remaining work
        from ember_to_angular.memory.migration_tracker import ArtifactStatus
        order = tracker_state.module_order
        remaining_modules = [
            m for m in order
            if any(
                tracker_state.artifacts.get(n) and
                tracker_state.artifacts[n].status != ArtifactStatus.VALIDATED
                for n in module_map.get(m, [])
            )
        ]

        validated = [
            n for n, rec in tracker_state.artifacts.items()
            if rec.status == ArtifactStatus.VALIDATED
        ]

        state = dict(initial_state)
        state.update({
            "artifact_registry": registry,
            "route_tree":        project.route_tree,
            "module_map":        module_map,
            "module_order":      order,
            "module_queue":      remaining_modules,
            "validated_names":   validated,
        })
        return state  # type: ignore[return-value]

    # ─────────────────────────────────────────────────────────────────────────
    # Report helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _partial_report(self) -> dict:
        state = self._tracker.state
        return {
            "phase":           state.phase.value,
            "ember_source":    self._ember_path,
            "angular_output":  self._angular_path,
            "total_artifacts": len(state.artifacts),
            "validated":       len(self._tracker.completed_artifacts()),
            "failed":          len(self._tracker.failed_artifacts()),
            "failed_artifacts":[a.name for a in self._tracker.failed_artifacts()],
        }

    @staticmethod
    def _print_report(report: dict):
        logger.info("=" * 60)
        logger.info("MIGRATION SUMMARY")
        logger.info("=" * 60)
        for k, v in report.items():
            logger.info("  %-25s: %s", k, v)
        logger.info("=" * 60)
