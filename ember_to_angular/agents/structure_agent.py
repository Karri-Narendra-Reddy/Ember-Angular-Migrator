"""
Structure Agent
───────────────
Creates the Angular project scaffolding aligned with the business use cases
discovered by the Analyzer Agent.  Writes all boilerplate files to disk
before any migration begins.

Model: gpt-4.1
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ember_to_angular.agents.base_agent import BaseAgent
from ember_to_angular.config.settings import MODEL_STRUCTURE, MAX_TOKENS_ANALYZER
from ember_to_angular.tools.angular_generator import (
    AngularScaffold,
    generate_app_routes,
    generate_app_config,
    generate_app_component,
    generate_main_ts,
    generate_tsconfig,
    generate_package_json,
)
from ember_to_angular.tools.file_writer import FileWriter
from ember_to_angular.tools.ember_parser import EmberProject

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert Angular architect specializing in migrating legacy Ember.js
applications to Angular 17+ standalone architecture.

Your responsibilities:
  1. Design a clean, domain-driven Angular project structure
  2. Map Ember route hierarchy → Angular lazy-loaded feature modules
  3. Plan shared/core module boundaries
  4. Generate accurate tsconfig paths, barrel exports, and scaffolding
  5. Ensure the structure accommodates all discovered business domains

Angular 17+ guidelines:
  • Standalone components (no NgModules unless forced)
  • Lazy-loaded routes with loadComponent / loadChildren
  • inject() function instead of constructor injection
  • Signals for local state, NgRx for complex shared state
  • HttpClient with interceptors for API calls
"""


class StructureAgent(BaseAgent):
    name          = "StructureAgent"
    deployment    = MODEL_STRUCTURE
    system_prompt = SYSTEM_PROMPT
    max_tokens    = MAX_TOKENS_ANALYZER

    def __init__(self, output_root: str, writer: FileWriter):
        super().__init__()
        self._output_root = output_root
        self._writer      = writer
        self._scaffold    = AngularScaffold(output_root)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def design_structure(self, project: EmberProject, project_analysis: dict) -> dict:
        """
        Ask the LLM to design the complete Angular directory structure.
        Returns a dict describing what was planned.
        """
        domains     = project_analysis.get("business_domains", [])
        module_order= project_analysis.get("migration_order", list(project.route_tree.keys()))
        route_tree  = json.dumps(project.route_tree, indent=2)

        prompt = f"""\
Design the complete Angular 17+ project structure for this migrated application.

## Business Domains Identified
{json.dumps(domains, indent=2)}

## Ember Route Tree (to be mapped to Angular lazy routes)
{route_tree}

## Angular Architecture Recommendation
{project_analysis.get("angular_architecture_recommendation", "standalone components")}

## Shared Dependencies
{json.dumps(project_analysis.get("shared_dependencies", []), indent=2)}

Produce a JSON structure plan:
{{
  "project_name": "...",
  "src_structure": {{
    "app": {{
      "core": ["interceptors/", "guards/", "models/"],
      "shared": ["components/", "pipes/", "directives/"],
      "features": {{
        "feature-name": ["component", "service", "routes"]
      }}
    }}
  }},
  "feature_modules": [
    {{
      "name": "...",
      "route_path": "...",
      "angular_path": "src/app/features/...",
      "components": ["..."],
      "services": ["..."],
      "models": ["..."]
    }}
  ],
  "shared_services": ["..."],
  "core_services": ["..."],
  "state_management": "NgRx|BehaviorSubject|Signal",
  "http_base_url_strategy": "environment|token"
}}
"""
        structure_plan = self.call_json(prompt, fresh=True)
        if not structure_plan:
            structure_plan = self._fallback_structure(project, module_order)
        logger.info("Angular structure designed: %d feature modules",
                    len(structure_plan.get("feature_modules", [])))
        return structure_plan

    def create_project_skeleton(self, project: EmberProject, structure_plan: dict) -> list[str]:
        """
        Writes all Angular scaffold files to disk.
        Returns list of created file paths.
        """
        created: list[str] = []

        # ── Top-level Angular files ───────────────────────────────────────────
        top_level = {
            "src/main.ts":           generate_main_ts(),
            "src/app/app.component.ts": generate_app_component(),
            "src/app/app.config.ts": generate_app_config(),
            "src/app/app.routes.ts": generate_app_routes(project.route_tree),
            "tsconfig.json":         generate_tsconfig(),
            "package.json":          generate_package_json(
                                         structure_plan.get("project_name", "migrated-app")
                                     ),
            "src/styles.scss":       "/* Global styles – migrated from Ember */\n",
            ".gitignore":            _gitignore(),
            "README.md":             self._generate_readme(structure_plan),
        }
        results = self._writer.write_many(top_level, overwrite=False)
        created.extend(k for k, ok in results.items() if ok)

        # ── Environment files ─────────────────────────────────────────────────
        env_files = {
            "src/environments/environment.ts":      _env_ts(production=False),
            "src/environments/environment.prod.ts": _env_ts(production=True),
        }
        results = self._writer.write_many(env_files, overwrite=False)
        created.extend(k for k, ok in results.items() if ok)

        # ── Feature module directories ────────────────────────────────────────
        for feature in structure_plan.get("feature_modules", []):
            feat_path = feature.get("angular_path", f"src/app/features/{feature['name']}")
            self._writer.ensure_dir(feat_path)

            # Component skeleton
            comp_file = self._scaffold.component_ts(
                name               = feature["name"],
                injected_services  = feature.get("services", []),
            )
            self._writer.write(comp_file.relative_path, comp_file.content, overwrite=False)
            created.append(comp_file.relative_path)

            html_file = self._scaffold.component_html(feature["name"])
            self._writer.write(html_file.relative_path, html_file.content, overwrite=False)
            created.append(html_file.relative_path)

            scss_file = self._scaffold.component_scss(feature["name"])
            self._writer.write(scss_file.relative_path, scss_file.content, overwrite=False)
            created.append(scss_file.relative_path)

            # Routes file
            route_file = self._scaffold.route_module(
                feature["name"],
                children=[c["name"] for c in structure_plan.get("feature_modules", [])
                          if c.get("parent") == feature["name"]]
            )
            self._writer.write(route_file.relative_path, route_file.content, overwrite=False)
            created.append(route_file.relative_path)

            # Service skeletons
            for svc in feature.get("services", []):
                svc_file = self._scaffold.service_ts(svc)
                self._writer.write(svc_file.relative_path, svc_file.content, overwrite=False)
                created.append(svc_file.relative_path)

            # Model skeletons
            for model in feature.get("models", []):
                model_file = self._scaffold.model_ts(model)
                self._writer.write(model_file.relative_path, model_file.content, overwrite=False)
                created.append(model_file.relative_path)

        # ── Shared module ─────────────────────────────────────────────────────
        self._create_shared_module(structure_plan, created)

        # ── Core services ─────────────────────────────────────────────────────
        for svc in structure_plan.get("core_services", []):
            svc_file = self._scaffold.service_ts(svc)
            self._writer.write(svc_file.relative_path, svc_file.content, overwrite=False)
            created.append(svc_file.relative_path)

        self._writer.flush_manifest()
        logger.info("Project skeleton created: %d files", len(created))
        return created

    def generate_angular_module_plan(self, module_name: str, module_plan: dict) -> str:
        """
        Generate detailed LLM guidance for migrating a specific module.
        Used by the Migration Agent.
        """
        prompt = f"""\
Generate detailed Angular implementation guidance for module "{module_name}".

## Module Plan
{json.dumps(module_plan, indent=2)}

Provide:
1. Exact Angular component/service structure
2. Route configuration
3. State management approach
4. API integration patterns
5. Template migration notes (Handlebars → Angular template syntax)
6. Key pitfalls to avoid

Be specific and actionable.  Include Angular code patterns where helpful.
"""
        return self.call(prompt, fresh=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _create_shared_module(self, structure_plan: dict, created: list[str]):
        shared_components = [
            "header", "footer", "sidebar", "loading-spinner",
            "error-message", "confirm-dialog",
        ]
        for comp in shared_components:
            comp_file = self._scaffold.component_ts(f"shared/{comp}")
            self._writer.write(comp_file.relative_path, comp_file.content, overwrite=False)
            created.append(comp_file.relative_path)

        for svc in structure_plan.get("shared_services", []):
            svc_file = self._scaffold.service_ts(svc)
            self._writer.write(svc_file.relative_path, svc_file.content, overwrite=False)
            created.append(svc_file.relative_path)

    def _generate_readme(self, structure_plan: dict) -> str:
        return f"""\
# Migrated Angular Application

Auto-generated by the Ember → Angular Migration Agent.

## Project Name
{structure_plan.get("project_name", "migrated-app")}

## State Management
{structure_plan.get("state_management", "TBD")}

## Feature Modules
{chr(10).join("- " + f["name"] for f in structure_plan.get("feature_modules", []))}

## Getting Started
```bash
npm install
ng serve
```

## Notes
- Migration was performed automatically; review all TODO comments
- Run `ng build --strict` before committing
"""

    @staticmethod
    def _fallback_structure(project: EmberProject, module_order: list[str]) -> dict:
        return {
            "project_name": "migrated-app",
            "feature_modules": [
                {"name": m, "route_path": m, "angular_path": f"src/app/{m}",
                 "components": [], "services": [], "models": []}
                for m in module_order
            ],
            "shared_services": [],
            "core_services": [],
            "state_management": "BehaviorSubject",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Static content helpers
# ─────────────────────────────────────────────────────────────────────────────

def _gitignore() -> str:
    return """\
# Angular
/dist
/.angular
/node_modules
*.js.map

# IDE
.vscode/
.idea/
*.swp
.DS_Store

# Environment
.env
.env.local

# Migration artifacts
migration_state.json
vector_store/
logs/
"""

def _env_ts(production: bool) -> str:
    return f"""\
export const environment = {{
  production: {str(production).lower()},
  apiBaseUrl: '{'' if production else 'http://localhost:4200'}/api',
}};
"""
