"""
LangGraph Migration Workflow
════════════════════════════
The entire Ember → Angular migration pipeline expressed as a LangGraph
StateGraph.  Each processing step is a node; routing decisions are
conditional edges.

Graph topology
──────────────
START
  → scan                    parse Ember project + index vector store
  → analyze_project         LLM: project overview & business domains
  → decide_order            LLM (o4-mini): optimal migration sequence
  → create_structure        LLM: design + write Angular skeleton
  → pick_module ──────────────────────────────────────────────┐
  → analyze_module          LLM: module-level plan             │
  → pick_artifact ──────────────────────────────────────────┐  │
  → analyze_artifact        LLM: deep per-artifact blueprint  │  │
  → migrate_artifact        LLM: generate Angular files       │  │
  → validate                LLM: cross-check vs Ember source  │  │
       ├─ passed ──────────────────────────────────────────── ┘  │
       ├─ needs_fix ─→ fix ─→ validate (retry loop)              │
       └─ failed ──────────────────────────────────────────── ┘  │
  → (no more artifacts) ─────────────────────────────────────────┘
  → (no more modules)  → finalize → END
"""

from __future__ import annotations

import json
import logging
import operator
import traceback
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ember_to_angular.agents.analyzer_agent import AnalyzerAgent
from ember_to_angular.agents.migration_agent import MigrationAgent
from ember_to_angular.agents.structure_agent import StructureAgent
from ember_to_angular.agents.validator_agent import ValidatorAgent
from ember_to_angular.memory.migration_tracker import (
    ArtifactStatus, MigrationPhase, MigrationTracker,
)
from ember_to_angular.memory.vector_store import CodeVectorStore
from ember_to_angular.tools.ember_parser import EmberArtifact, EmberParser, EmberProject
from ember_to_angular.tools.file_reader import LargeFileReader
from ember_to_angular.tools.file_writer import FileWriter

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


# ─────────────────────────────────────────────────────────────────────────────
# Shared graph state
# ─────────────────────────────────────────────────────────────────────────────

def _merge_dicts(a: dict, b: dict) -> dict:
    """Reducer: merge two dicts (b wins on collision)."""
    return {**a, **b}


class MigrationGraphState(TypedDict):
    # ── Inputs ─────────────────────────────────────────────────────────────────
    ember_path:   str
    angular_path: str

    # ── Parsed project (serialisable subset) ───────────────────────────────────
    artifact_registry: dict          # name → {type, file_path, template_path, ...}
    route_tree:        dict          # ember route hierarchy
    module_map:        dict          # module_name → [artifact_names]

    # ── Analysis ───────────────────────────────────────────────────────────────
    project_analysis: dict
    structure_plan:   dict

    # ── Queue management ───────────────────────────────────────────────────────
    module_order:    list[str]       # ordered list of module names to migrate
    module_queue:    list[str]       # remaining modules (consumed FIFO)
    artifact_queue:  list[str]       # artifact names in the current module (FIFO)

    # ── Current context ────────────────────────────────────────────────────────
    current_module:        str
    current_artifact_name: str
    artifact_blueprint:    dict      # LLM analysis of current artifact
    retry_count:           int

    # ── Accumulated outputs (reducer: merge / append) ──────────────────────────
    migrated_files:    Annotated[dict, _merge_dicts]   # rel_path → content
    validated_names:   Annotated[list[str], operator.add]
    failed_names:      Annotated[list[str], operator.add]
    errors:            Annotated[list[str], operator.add]

    # ── Validation pass results ────────────────────────────────────────────────
    validation_passed: bool
    validation_issues: list[str]

    # ── Final ──────────────────────────────────────────────────────────────────
    report: dict


# ─────────────────────────────────────────────────────────────────────────────
# Graph factory
# ─────────────────────────────────────────────────────────────────────────────

def build_migration_graph(
    analyzer:  AnalyzerAgent,
    structure: StructureAgent,
    migrator:  MigrationAgent,
    validator: ValidatorAgent,
    parser:    EmberParser,
    reader:    LargeFileReader,
    vs:        CodeVectorStore,
    tracker:   MigrationTracker,
    writer:    FileWriter,
):
    """
    Build and compile the LangGraph StateGraph.

    All agent instances are captured in node-function closures so the graph
    state stays JSON-serialisable.
    """

    # ── Helper: retrieve full EmberArtifact from registry ─────────────────────

    def _get_artifact(state: MigrationGraphState, name: str) -> EmberArtifact | None:
        reg = state["artifact_registry"].get(name)
        if not reg:
            return None
        a = EmberArtifact(
            name          = name,
            artifact_type = reg["type"],
            file_path     = reg["file_path"],
            template_path = reg.get("template_path"),
        )
        a.imports             = reg.get("imports", [])
        a.service_injections  = reg.get("service_injections", [])
        a.actions             = reg.get("actions", [])
        a.computed_props      = reg.get("computed_props", [])
        a.lifecycle_hooks     = reg.get("lifecycle_hooks", [])
        a.model_attrs         = reg.get("model_attrs", [])
        a.template_components = reg.get("template_components", [])
        return a

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 1 – scan
    # ─────────────────────────────────────────────────────────────────────────

    def scan_node(state: MigrationGraphState) -> dict:
        logger.info("[GRAPH] scan_node: parsing Ember project at %s", state["ember_path"])
        tracker.transition(MigrationPhase.SCANNING)

        project: EmberProject = parser.parse(state["ember_path"])
        logger.info(project.summary())

        # Index all source files into vector store
        scan_results = reader.scan_directory(
            state["ember_path"], extensions=[".js", ".ts", ".hbs"]
        )
        indexed = vs.index_scan_results(scan_results, prefix="ember::")
        vs.save()
        logger.info("[scan] Indexed %d chunks into vector store", indexed)

        # Build serialisable artifact registry
        registry: dict[str, dict] = {}
        for a in project.artifacts:
            registry[a.name] = {
                "type":                a.artifact_type,
                "file_path":           a.file_path,
                "template_path":       a.template_path,
                "imports":             a.imports,
                "service_injections":  a.service_injections,
                "actions":             a.actions,
                "computed_props":      a.computed_props,
                "lifecycle_hooks":     a.lifecycle_hooks,
                "model_attrs":         a.model_attrs,
                "template_components": a.template_components,
            }

        # Serialisable module map
        module_map: dict[str, list[str]] = {}
        for mod_name, arts in project.modules.items():
            module_map[mod_name] = [a.name for a in arts]

        # Register with tracker
        tracker.register_artifacts([
            {
                "name":         a.name,
                "type":         a.artifact_type,
                "files":        [a.file_path] + ([a.template_path] if a.template_path else []),
                "dependencies": project.dependency_graph.get(a.name, []),
            }
            for a in project.artifacts
        ])

        tracker.transition(MigrationPhase.SCANNED)
        return {
            "artifact_registry": registry,
            "route_tree":        project.route_tree,
            "module_map":        module_map,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 2 – analyze_project
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_project_node(state: MigrationGraphState) -> dict:
        logger.info("[GRAPH] analyze_project_node")

        # Reconstruct a minimal EmberProject for the analyzer
        from ember_to_angular.tools.ember_parser import EmberProject as EP
        project_stub = EP(root_path=state["ember_path"])
        for name, reg in state["artifact_registry"].items():
            a = EmberArtifact(
                name=name, artifact_type=reg["type"], file_path=reg["file_path"]
            )
            project_stub.artifacts.append(a)
        project_stub.route_tree = state["route_tree"]
        project_stub.modules    = {
            mod: [project_stub.find(n) or EmberArtifact(n, "unknown", "")
                  for n in names]
            for mod, names in state["module_map"].items()
        }

        analysis = analyzer.analyze_project(project_stub)
        return {"project_analysis": analysis}

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 3 – decide_order  (o4-mini reasoning)
    # ─────────────────────────────────────────────────────────────────────────

    def decide_order_node(state: MigrationGraphState) -> dict:
        logger.info("[GRAPH] decide_order_node")
        modules = list(state["module_map"].keys())
        analysis = state["project_analysis"]

        # Use the orchestrator's reasoning to pick order
        from ember_to_angular.agents.base_agent import BaseAgent
        from ember_to_angular.config.settings import MODEL_ORCHESTRATOR, MAX_TOKENS_ORCHESTRATOR

        class _Decider(BaseAgent):
            name          = "DecisionNode"
            deployment    = MODEL_ORCHESTRATOR
            system_prompt = "You are an Ember→Angular migration planner. Return only JSON arrays."
            max_tokens    = MAX_TOKENS_ORCHESTRATOR
            use_reasoning = True

        decider = _Decider()
        prompt = f"""\
Order these Ember modules for migration. Dependencies first, shared before features.

Modules: {json.dumps(modules)}
Project analysis: {json.dumps(analysis, indent=2)[:3000]}

Return a JSON array of module names in migration order.
"""
        result = decider.call_json(prompt, fresh=True)
        order  = result if isinstance(result, list) else modules
        logger.info("[decide_order] Order: %s", order[:5])
        tracker.set_module_order(order)
        return {
            "module_order": order,
            "module_queue": list(order),      # start with a full copy
        }

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 4 – create_structure
    # ─────────────────────────────────────────────────────────────────────────

    def create_structure_node(state: MigrationGraphState) -> dict:
        if tracker.state.angular_structure_created:
            logger.info("[GRAPH] create_structure_node: already done, skipping")
            return {}

        logger.info("[GRAPH] create_structure_node")
        tracker.transition(MigrationPhase.STRUCTURING)

        # Reconstruct project stub for structure agent
        from ember_to_angular.tools.ember_parser import EmberProject as EP
        project_stub = EP(root_path=state["ember_path"])
        project_stub.route_tree = state["route_tree"]
        project_stub.modules    = {
            mod: [EmberArtifact(n, state["artifact_registry"].get(n, {}).get("type", "unknown"), "")
                  for n in names]
            for mod, names in state["module_map"].items()
        }

        plan   = structure.design_structure(project_stub, state["project_analysis"])
        structure.create_project_skeleton(project_stub, plan)
        tracker.mark_structure_created()
        tracker.transition(MigrationPhase.STRUCTURED)
        return {"structure_plan": plan}

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 5 – pick_module
    # ─────────────────────────────────────────────────────────────────────────

    def pick_module_node(state: MigrationGraphState) -> dict:
        queue = list(state.get("module_queue", []))
        if not queue:
            logger.info("[GRAPH] pick_module_node: no more modules")
            return {"current_module": "__done__", "module_queue": []}

        module_name = queue.pop(0)
        artifact_names = state["module_map"].get(module_name, [])

        # Filter already-validated artifacts
        pending = [
            n for n in artifact_names
            if tracker.state.artifacts.get(n) and
               tracker.state.artifacts[n].status != ArtifactStatus.VALIDATED
        ]

        logger.info("[GRAPH] pick_module_node: %s (%d artifacts pending)", module_name, len(pending))
        tracker.set_current_module(module_name)
        tracker.transition(MigrationPhase.MIGRATING)

        return {
            "module_queue":   queue,
            "current_module": module_name,
            "artifact_queue": _sort_artifacts(pending, state["artifact_registry"]),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 6 – analyze_module
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_module_node(state: MigrationGraphState) -> dict:
        module_name = state["current_module"]
        logger.info("[GRAPH] analyze_module_node: %s", module_name)

        artifact_names = state["module_map"].get(module_name, [])
        artifacts = [a for a in (_get_artifact(state, n) for n in artifact_names) if a]

        module_analysis = analyzer.analyze_module(module_name, artifacts)
        guidance        = structure.generate_angular_module_plan(module_name, module_analysis)

        # Store guidance as a simple string inside module state (via errors list trick – cleaner via extra key)
        # We store it serialised in the artifact_registry under a special key
        registry_update = dict(state["artifact_registry"])
        registry_update[f"__module_guidance_{module_name}__"] = {"type": "_guidance", "guidance": guidance, "file_path": ""}

        return {"artifact_registry": registry_update}

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 7 – pick_artifact
    # ─────────────────────────────────────────────────────────────────────────

    def pick_artifact_node(state: MigrationGraphState) -> dict:
        queue = list(state.get("artifact_queue", []))
        if not queue:
            logger.info("[GRAPH] pick_artifact_node: module %s done", state["current_module"])
            return {"current_artifact_name": "__done__", "artifact_queue": []}

        name = queue.pop(0)
        logger.info("[GRAPH] pick_artifact_node: %s", name)
        tracker.update_artifact(name, status=ArtifactStatus.ANALYZING)
        return {
            "artifact_queue":       queue,
            "current_artifact_name": name,
            "retry_count":           0,
            "validation_passed":     False,
            "validation_issues":     [],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 8 – analyze_artifact
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_artifact_node(state: MigrationGraphState) -> dict:
        name     = state["current_artifact_name"]
        artifact = _get_artifact(state, name)
        logger.info("[GRAPH] analyze_artifact_node: %s", name)

        try:
            blueprint = analyzer.analyze_artifact(artifact)
            tracker.update_artifact(name, status=ArtifactStatus.ANALYZED,
                                    analysis_summary=blueprint.get("business_purpose", ""))
        except Exception:
            logger.error("[analyze_artifact] failed for %s:\n%s", name, traceback.format_exc())
            tracker.update_artifact(name, status=ArtifactStatus.FAILED)
            blueprint = {}

        return {"artifact_blueprint": blueprint}

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 9 – migrate_artifact
    # ─────────────────────────────────────────────────────────────────────────

    def migrate_artifact_node(state: MigrationGraphState) -> dict:
        name      = state["current_artifact_name"]
        artifact  = _get_artifact(state, name)
        blueprint = state.get("artifact_blueprint", {})
        module    = state["current_module"]

        # Retrieve module guidance from registry
        guidance_key = f"__module_guidance_{module}__"
        guidance = state["artifact_registry"].get(guidance_key, {}).get("guidance", "")

        logger.info("[GRAPH] migrate_artifact_node: %s", name)
        tracker.update_artifact(name, status=ArtifactStatus.MIGRATING)

        try:
            files = migrator.migrate_artifact(artifact, blueprint, guidance)
            tracker.update_artifact(name, status=ArtifactStatus.MIGRATED,
                                    angular_files=list(files.keys()))
            return {"migrated_files": files}
        except Exception:
            logger.error("[migrate_artifact] failed for %s:\n%s", name, traceback.format_exc())
            tracker.update_artifact(name, status=ArtifactStatus.FAILED)
            return {"errors": [f"Migration failed: {name}"]}

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 10 – validate
    # ─────────────────────────────────────────────────────────────────────────

    def validate_node(state: MigrationGraphState) -> dict:
        name      = state["current_artifact_name"]
        artifact  = _get_artifact(state, name)
        blueprint = state.get("artifact_blueprint", {})

        # Gather only the files belonging to this artifact
        artifact_files = {
            k: v for k, v in state.get("migrated_files", {}).items()
            if name.replace("/", "-").lower() in k.lower() or
               name.split("/")[-1].lower() in k.lower()
        }
        if not artifact_files:
            artifact_files = dict(state.get("migrated_files", {}))

        logger.info("[GRAPH] validate_node: %s", name)
        tracker.update_artifact(name, status=ArtifactStatus.VALIDATING)

        try:
            result = validator.validate(artifact, artifact_files, blueprint)
            issues = [i.description for i in result.issues if i.severity == "error"]

            if result.passed:
                tracker.update_artifact(name, status=ArtifactStatus.VALIDATED, validation_issues=[])
                return {
                    "validation_passed": True,
                    "validation_issues": [],
                    "validated_names":   [name],
                }
            else:
                tracker.update_artifact(name, status=ArtifactStatus.NEEDS_FIX,
                                        validation_issues=issues)
                return {
                    "validation_passed": False,
                    "validation_issues": issues,
                }
        except Exception:
            logger.error("[validate] failed for %s:\n%s", name, traceback.format_exc())
            tracker.update_artifact(name, status=ArtifactStatus.FAILED)
            return {
                "validation_passed": False,
                "validation_issues": ["Validator threw an exception"],
                "errors": [f"Validation error: {name}"],
            }

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 11 – fix_artifact
    # ─────────────────────────────────────────────────────────────────────────

    def fix_artifact_node(state: MigrationGraphState) -> dict:
        name      = state["current_artifact_name"]
        artifact  = _get_artifact(state, name)
        blueprint = state.get("artifact_blueprint", {})

        logger.info("[GRAPH] fix_artifact_node: %s (retry %d)", name, state.get("retry_count", 0))
        tracker.increment_retry(name)

        # Rebuild a minimal ValidationResult for the fixer
        from ember_to_angular.agents.validator_agent import ValidationResult, ValidationIssue
        issues = [
            ValidationIssue(
                severity="error", file=name,
                line_hint="", description=d, suggestion="",
            )
            for d in state.get("validation_issues", [])
        ]
        fake_result = ValidationResult(
            artifact_name=name, passed=False,
            issues=issues, score=0,
        )

        artifact_files = {
            k: v for k, v in state.get("migrated_files", {}).items()
            if name.replace("/", "-").lower() in k.lower() or
               name.split("/")[-1].lower() in k.lower()
        }

        try:
            fixed_files = validator.fix_issues(artifact, artifact_files, fake_result)
            for rel_path, content in fixed_files.items():
                writer.write(rel_path, content, overwrite=True)
            return {
                "migrated_files": fixed_files,
                "retry_count":    state.get("retry_count", 0) + 1,
            }
        except Exception:
            logger.error("[fix_artifact] failed for %s:\n%s", name, traceback.format_exc())
            return {"retry_count": state.get("retry_count", 0) + 1}

    # ─────────────────────────────────────────────────────────────────────────
    # NODE 12 – finalize
    # ─────────────────────────────────────────────────────────────────────────

    def finalize_node(state: MigrationGraphState) -> dict:
        logger.info("[GRAPH] finalize_node")
        tracker.transition(MigrationPhase.DONE)
        writer.flush_manifest()

        validated = state.get("validated_names", [])
        failed    = state.get("failed_names", [])
        total     = len(state.get("artifact_registry", {}))

        report = {
            "phase":            "done",
            "ember_source":     state["ember_path"],
            "angular_output":   state["angular_path"],
            "total_artifacts":  total,
            "validated":        len(validated),
            "failed":           len(failed),
            "failed_artifacts": failed,
            "vector_store_size": vs.size,
        }
        logger.info("Migration complete: %s", json.dumps(report, indent=2))
        return {"report": report}

    # ─────────────────────────────────────────────────────────────────────────
    # Conditional edge functions
    # ─────────────────────────────────────────────────────────────────────────

    def route_after_pick_module(state: MigrationGraphState) -> str:
        if state.get("current_module") == "__done__":
            return "finalize"
        return "analyze_module"

    def route_after_pick_artifact(state: MigrationGraphState) -> str:
        name = state.get("current_artifact_name", "")
        if name == "__done__":
            return "pick_module"
        rec = tracker.state.artifacts.get(name)
        if rec and rec.status == ArtifactStatus.FAILED:
            return "pick_artifact"    # skip this one, take the next
        return "analyze_artifact"

    def route_after_validate(state: MigrationGraphState) -> str:
        if state.get("validation_passed"):
            return "pick_artifact"
        retry = state.get("retry_count", 0)
        if retry < MAX_RETRIES:
            return "fix_artifact"
        # Out of retries → mark failed, continue
        name = state.get("current_artifact_name", "")
        if name and name != "__done__":
            tracker.update_artifact(name, status=ArtifactStatus.FAILED)
        return "pick_artifact"

    def route_after_fix(state: MigrationGraphState) -> str:
        # Always re-validate after a fix
        return "validate"

    def route_after_analyze_artifact(state: MigrationGraphState) -> str:
        # If analysis produced an empty blueprint (failure), skip migration
        if not state.get("artifact_blueprint"):
            return "pick_artifact"
        return "migrate_artifact"

    # ─────────────────────────────────────────────────────────────────────────
    # Assemble the graph
    # ─────────────────────────────────────────────────────────────────────────

    builder = StateGraph(MigrationGraphState)

    # Nodes
    builder.add_node("scan",             scan_node)
    builder.add_node("analyze_project",  analyze_project_node)
    builder.add_node("decide_order",     decide_order_node)
    builder.add_node("create_structure", create_structure_node)
    builder.add_node("pick_module",      pick_module_node)
    builder.add_node("analyze_module",   analyze_module_node)
    builder.add_node("pick_artifact",    pick_artifact_node)
    builder.add_node("analyze_artifact", analyze_artifact_node)
    builder.add_node("migrate_artifact", migrate_artifact_node)
    builder.add_node("validate",         validate_node)
    builder.add_node("fix_artifact",     fix_artifact_node)
    builder.add_node("finalize",         finalize_node)

    # Unconditional edges
    builder.add_edge(START,            "scan")
    builder.add_edge("scan",           "analyze_project")
    builder.add_edge("analyze_project","decide_order")
    builder.add_edge("decide_order",   "create_structure")
    builder.add_edge("create_structure","pick_module")
    builder.add_edge("analyze_module", "pick_artifact")
    builder.add_edge("migrate_artifact","validate")
    builder.add_edge("finalize",       END)

    # Conditional edges
    builder.add_conditional_edges(
        "pick_module",
        route_after_pick_module,
        {"analyze_module": "analyze_module", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "pick_artifact",
        route_after_pick_artifact,
        {
            "analyze_artifact": "analyze_artifact",
            "pick_module":      "pick_module",
            "pick_artifact":    "pick_artifact",
        },
    )
    builder.add_conditional_edges(
        "analyze_artifact",
        route_after_analyze_artifact,
        {"migrate_artifact": "migrate_artifact", "pick_artifact": "pick_artifact"},
    )
    builder.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "pick_artifact": "pick_artifact",
            "fix_artifact":  "fix_artifact",
        },
    )
    builder.add_conditional_edges(
        "fix_artifact",
        route_after_fix,
        {"validate": "validate"},
    )

    return builder.compile()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sort_artifacts(names: list[str], registry: dict) -> list[str]:
    """Sort artifact names: models → services → components (dependency order)."""
    priority = {
        "model": 0, "service": 1, "mixin": 2, "helper": 3,
        "adapter": 4, "serializer": 5, "controller": 6, "route": 7, "component": 8,
    }
    return sorted(
        names,
        key=lambda n: priority.get(registry.get(n, {}).get("type", "z"), 99),
    )
