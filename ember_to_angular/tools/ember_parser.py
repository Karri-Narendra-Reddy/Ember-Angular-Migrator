"""
Ember project parser – statically analyses an Ember.js codebase and produces
a rich dependency graph without executing any JavaScript.

Extracts
────────
  • Routes         (router.js / router.ts)
  • Components     (.hbs templates + backing .js/.ts)
  • Services       (app/services/**)
  • Models         (app/models/**)
  • Controllers    (app/controllers/**)
  • Adapters       (app/adapters/**)
  • Serializers    (app/serializers/**)
  • Helpers        (app/helpers/**)
  • Mixins         (app/mixins/**)
  • Config files   (config/**)

For each artifact we capture
  • file path
  • ember type
  • name (camelCase & kebab-case)
  • imports / dependencies
  • actions exposed
  • computed properties
  • lifecycle hooks used
  • template tags referenced  (for components/routes)
  • service injections
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional

from ember_to_angular.tools.file_reader import LargeFileReader, ScanResult
from ember_to_angular.config.settings import EMBER_IGNORE_DIRS

# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EmberArtifact:
    name: str
    artifact_type: str           # route | component | service | model | …
    file_path: str
    template_path: Optional[str] = None
    imports: List[str]           = field(default_factory=list)
    service_injections: List[str]= field(default_factory=list)
    actions: List[str]           = field(default_factory=list)
    computed_props: List[str]    = field(default_factory=list)
    lifecycle_hooks: List[str]   = field(default_factory=list)
    model_attrs: List[str]       = field(default_factory=list)
    template_components: List[str]=field(default_factory=list)
    route_children: List[str]    = field(default_factory=list)
    raw_snippet: str             = ""     # representative code snippet for LLM context

    def summary(self) -> str:
        lines = [
            f"[{self.artifact_type.upper()}] {self.name}",
            f"  file: {self.file_path}",
        ]
        if self.template_path:
            lines.append(f"  template: {self.template_path}")
        if self.imports:
            lines.append(f"  imports: {', '.join(self.imports[:8])}")
        if self.service_injections:
            lines.append(f"  services: {', '.join(self.service_injections)}")
        if self.actions:
            lines.append(f"  actions: {', '.join(self.actions[:10])}")
        if self.computed_props:
            lines.append(f"  computed: {', '.join(self.computed_props[:10])}")
        if self.lifecycle_hooks:
            lines.append(f"  hooks: {', '.join(self.lifecycle_hooks)}")
        if self.model_attrs:
            lines.append(f"  model attrs: {', '.join(self.model_attrs)}")
        if self.template_components:
            lines.append(f"  uses components: {', '.join(self.template_components[:10])}")
        return "\n".join(lines)


@dataclass
class EmberProject:
    root_path: str
    artifacts: List[EmberArtifact]     = field(default_factory=list)
    route_tree: Dict[str, list]        = field(default_factory=dict)
    dependency_graph: Dict[str, list]  = field(default_factory=dict)
    modules: Dict[str, List[EmberArtifact]] = field(default_factory=dict)

    # Convenience
    def by_type(self, artifact_type: str) -> List[EmberArtifact]:
        return [a for a in self.artifacts if a.artifact_type == artifact_type]

    def find(self, name: str) -> Optional[EmberArtifact]:
        for a in self.artifacts:
            if a.name == name:
                return a
        return None

    def summary(self) -> str:
        counts: Dict[str, int] = {}
        for a in self.artifacts:
            counts[a.artifact_type] = counts.get(a.artifact_type, 0) + 1
        lines = [f"Ember project: {self.root_path}", f"Total artifacts: {len(self.artifacts)}"]
        for t, c in sorted(counts.items()):
            lines.append(f"  {t}: {c}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────────────────────────────────────

RE_IMPORT      = re.compile(r"^import\s+.+\s+from\s+['\"](.+)['\"]", re.MULTILINE)
RE_SERVICE_INJ = re.compile(r"(\w+)\s*=\s*service\(\s*['\"]?([\w/-]*)['\"]?\s*\)", re.MULTILINE)
RE_ACTION      = re.compile(r"(?:actions\s*[=:]\s*\{[^}]*?|\bactions\b[^{]*?\{)(.*?)(?=\n\s*\})", re.DOTALL)
RE_ACTION_NAME = re.compile(r"^\s{2,}(\w+)\s*[\(:({]", re.MULTILINE)
RE_COMPUTED    = re.compile(r"(\w+)\s*=\s*computed\(", re.MULTILINE)
RE_LIFECYCLE   = re.compile(
    r"\b(init|didInsertElement|willDestroyElement|didReceiveAttrs|"
    r"didUpdate|willDestroy|model|beforeModel|afterModel|"
    r"setupController|renderTemplate)\s*\(", re.MULTILINE
)
RE_DS_ATTR     = re.compile(r"(\w+)\s*=\s*DS\.attr\(|attr\(['\"](\w+)['\"]", re.MULTILINE)
RE_HBS_COMPONENT = re.compile(r"<([A-Z][A-Za-z0-9::-]+)", re.MULTILINE)
RE_HBS_HELPER    = re.compile(r"\{\{([\w-]+)\s", re.MULTILINE)
RE_ROUTE_MAP   = re.compile(
    r"this\.route\(\s*['\"]([^'\"]+)['\"]([^)]*)\)", re.MULTILINE
)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

class EmberParser:
    """
    Statically parses an Ember project directory and returns an EmberProject
    with all artifacts and their dependency relationships.
    """

    ARTIFACT_DIR_MAP = {
        "routes":      "route",
        "components":  "component",
        "services":    "service",
        "models":      "model",
        "controllers": "controller",
        "adapters":    "adapter",
        "serializers": "serializer",
        "helpers":     "helper",
        "mixins":      "mixin",
    }

    LIFECYCLE_KEYWORDS = [
        "init", "didInsertElement", "willDestroyElement", "didReceiveAttrs",
        "didUpdate", "willDestroy", "model", "beforeModel", "afterModel",
        "setupController", "renderTemplate",
    ]

    def __init__(self, reader: LargeFileReader | None = None):
        self._reader = reader or LargeFileReader()

    def parse(self, root_path: str) -> EmberProject:
        """
        Full parse of the Ember project rooted at `root_path`.
        Returns EmberProject with all artifacts, dependency graph, and route tree.
        """
        root = Path(root_path)
        project = EmberProject(root_path=root_path)

        app_dir = root / "app"
        if not app_dir.exists():
            app_dir = root   # non-standard layout – scan root

        for subdir, atype in self.ARTIFACT_DIR_MAP.items():
            target = app_dir / subdir
            if not target.exists():
                continue
            for fpath in self._walk(target, exts={".js", ".ts"}):
                artifact = self._parse_js_file(fpath, atype, root)
                if artifact:
                    project.artifacts.append(artifact)

        # Templates (HBS)
        self._attach_templates(project, app_dir)

        # Router
        for router_file in ["router.js", "router.ts"]:
            rp = app_dir / router_file
            if rp.exists():
                self._parse_router(rp, project)
                break

        # Build dependency graph
        self._build_dependency_graph(project)

        # Group into logical modules (by route top-level)
        self._build_modules(project)

        return project

    # ── JS / TS file parsing ───────────────────────────────────────────────────

    def _parse_js_file(self, fpath: Path, atype: str, root: Path) -> EmberArtifact | None:
        result: ScanResult = self._reader.read(fpath)
        if result.error:
            return None

        content = result.full_content()
        name    = self._derive_name(fpath, root)

        artifact = EmberArtifact(
            name          = name,
            artifact_type = atype,
            file_path     = str(fpath),
        )

        artifact.imports           = self._extract_imports(content)
        artifact.service_injections= self._extract_services(content)
        artifact.actions           = self._extract_actions(content)
        artifact.computed_props    = self._extract_computed(content)
        artifact.lifecycle_hooks   = self._extract_lifecycle(content)

        if atype == "model":
            artifact.model_attrs = self._extract_model_attrs(content)

        # Keep a representative snippet (first 60 lines)
        first_chunk_lines = content.splitlines()[:60]
        artifact.raw_snippet = "\n".join(first_chunk_lines)

        return artifact

    # ── Template analysis ──────────────────────────────────────────────────────

    def _attach_templates(self, project: EmberProject, app_dir: Path):
        templates_dir = app_dir / "templates"
        if not templates_dir.exists():
            return

        # Map component/route name → template path
        hbs_map: dict[str, Path] = {}
        for fpath in self._walk(templates_dir, exts={".hbs"}):
            rel = fpath.relative_to(templates_dir)
            key = str(rel.with_suffix("")).replace(os.sep, "/")
            hbs_map[key] = fpath

        for artifact in project.artifacts:
            template_key = artifact.name.replace(".", "/")
            if template_key in hbs_map:
                tp = hbs_map[template_key]
                artifact.template_path = str(tp)
                # Parse HBS for component references
                result = self._reader.read(tp)
                if not result.error:
                    hbs_content = result.full_content()
                    artifact.template_components = self._extract_hbs_components(hbs_content)

        # Components in app/components may have co-located templates
        components_dir = app_dir / "components"
        if components_dir.exists():
            for fpath in self._walk(components_dir, exts={".hbs"}):
                name = self._derive_name(fpath.with_suffix(""), app_dir)
                # Find matching artifact
                for artifact in project.artifacts:
                    if artifact.artifact_type == "component" and artifact.name == name:
                        artifact.template_path = str(fpath)
                        result = self._reader.read(fpath)
                        if not result.error:
                            artifact.template_components = self._extract_hbs_components(
                                result.full_content()
                            )

    # ── Router ─────────────────────────────────────────────────────────────────

    def _parse_router(self, router_file: Path, project: EmberProject):
        result = self._reader.read(router_file)
        if result.error:
            return
        content = result.full_content()
        matches = RE_ROUTE_MAP.findall(content)
        for route_name, options in matches:
            project.route_tree[route_name] = []
            # Look for nested routes (simple heuristic)
            child_pat = re.compile(
                rf"this\.route\(['\"]({re.escape(route_name)}[^'\"]*)['\"]",
                re.MULTILINE,
            )
            for child in child_pat.findall(content):
                if child != route_name:
                    project.route_tree[route_name].append(child)

    # ── Dependency graph ───────────────────────────────────────────────────────

    def _build_dependency_graph(self, project: EmberProject):
        name_set = {a.name for a in project.artifacts}
        for artifact in project.artifacts:
            deps: list[str] = []
            for imp in artifact.imports:
                # Match import path to known artifact name
                candidate = imp.split("/")[-1]
                if candidate in name_set:
                    deps.append(candidate)
            for svc in artifact.service_injections:
                if svc in name_set:
                    deps.append(svc)
            project.dependency_graph[artifact.name] = list(set(deps))

    # ── Module grouping ────────────────────────────────────────────────────────

    def _build_modules(self, project: EmberProject):
        """Group artifacts into logical modules based on route top-levels."""
        for route_name in project.route_tree:
            module_artifacts = []
            for artifact in project.artifacts:
                if artifact.name.startswith(route_name) or \
                   any(route_name in imp for imp in artifact.imports):
                    module_artifacts.append(artifact)
            if module_artifacts:
                project.modules[route_name] = module_artifacts

        # Artifacts not in any module go to "shared"
        assigned = {a for group in project.modules.values() for a in group}
        shared = [a for a in project.artifacts if a not in assigned]
        if shared:
            project.modules["shared"] = shared

    # ── Static extractors ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_imports(content: str) -> list[str]:
        return RE_IMPORT.findall(content)

    @staticmethod
    def _extract_services(content: str) -> list[str]:
        matches = RE_SERVICE_INJ.findall(content)
        return [m[1] or m[0] for m in matches if m[0] or m[1]]

    @staticmethod
    def _extract_actions(content: str) -> list[str]:
        actions_block = RE_ACTION.search(content)
        if not actions_block:
            return []
        return RE_ACTION_NAME.findall(actions_block.group(0))

    @staticmethod
    def _extract_computed(content: str) -> list[str]:
        return RE_COMPUTED.findall(content)

    @staticmethod
    def _extract_lifecycle(content: str) -> list[str]:
        return list({m.group(1) for m in RE_LIFECYCLE.finditer(content)})

    @staticmethod
    def _extract_model_attrs(content: str) -> list[str]:
        matches = RE_DS_ATTR.findall(content)
        return [m[0] or m[1] for m in matches if m[0] or m[1]]

    @staticmethod
    def _extract_hbs_components(hbs_content: str) -> list[str]:
        angle_syntax = RE_HBS_COMPONENT.findall(hbs_content)
        curly_syntax = RE_HBS_HELPER.findall(hbs_content)
        return list(set(angle_syntax + curly_syntax))

    @staticmethod
    def _derive_name(fpath: Path, root: Path) -> str:
        try:
            rel = fpath.relative_to(root / "app")
            # Drop the first directory segment (routes/, components/, …)
            parts = rel.parts[1:]
            return "/".join(parts).replace(".js", "").replace(".ts", "")
        except ValueError:
            return fpath.stem

    @staticmethod
    def _walk(directory: Path, exts: set[str]) -> list[Path]:
        results: list[Path] = []
        ignore = set(EMBER_IGNORE_DIRS)
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [d for d in dirnames if d not in ignore]
            for fname in filenames:
                p = Path(dirpath) / fname
                if p.suffix in exts:
                    results.append(p)
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Convenience singleton
# ─────────────────────────────────────────────────────────────────────────────

_default_parser = EmberParser()

def parse_ember_project(root_path: str) -> EmberProject:
    return _default_parser.parse(root_path)
