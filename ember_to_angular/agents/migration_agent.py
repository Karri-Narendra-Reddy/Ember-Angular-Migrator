"""
Migration Agent
───────────────
The core workhorse.  Migrates Ember artifacts one at a time to Angular,
ensuring:
  • Business logic is exactly preserved
  • Only the files belonging to the current artifact are touched
  • Large files are processed chunk-by-chunk with full context continuity
  • Template (HBS) → Angular HTML is done in a separate focused pass
  • Ember-Data models → TypeScript interfaces + Angular services

Model: gpt-5  (best code generation capability)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from ember_to_angular.agents.base_agent import BaseAgent
from ember_to_angular.config.settings import MODEL_MIGRATION, MAX_TOKENS_MIGRATION
from ember_to_angular.memory.vector_store import CodeVectorStore
from ember_to_angular.tools.ember_parser import EmberArtifact
from ember_to_angular.tools.file_reader import LargeFileReader
from ember_to_angular.tools.file_writer import FileWriter
from ember_to_angular.tools.angular_generator import (
    kebab_to_pascal, pascal_to_kebab, kebab_to_camel,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a world-class Angular 17+ migration engineer specializing in converting
Ember.js 3.x / 4.x applications to Angular 17+ standalone architecture.

Core principles you ALWAYS follow:
  1. PRESERVE every business rule, formula, validation, and workflow.
     Do not simplify, optimise, or remove logic unless it is pure Ember boilerplate.
  2. Map Ember concepts precisely:
     - Ember Service          → @Injectable({ providedIn: 'root' }) Service
     - Ember Route model()    → Angular Resolver + component ngOnInit()
     - Ember computed()       → Angular Signal / computed() / getter / pipe
     - Ember action           → Angular method (possibly with (click) binding)
     - Ember observer         → RxJS subscription / effect()
     - Ember Component        → Angular standalone component
     - Handlebars {{#if}}     → *ngIf / @if
     - Handlebars {{#each}}   → *ngFor / @for
     - Ember Data model       → TypeScript interface + HttpClient service
     - Ember Data store       → Angular service with HttpClient
     - Ember mixin            → Angular abstract class / utility service
  3. Use Angular 17+ syntax:
     - inject() instead of constructor injection
     - Signals for reactive state where appropriate
     - Standalone components (no NgModules)
     - @if, @for, @switch control flow (new template syntax)
  4. TypeScript strict mode:
     - All variables explicitly typed
     - No 'any' unless the source is truly dynamic
  5. When migrating templates:
     - Preserve all CSS class bindings, event bindings, data bindings
     - Convert {{component-name args}} → <app-component-name [input]="val">
     - Preserve all conditional rendering logic
  6. Keep TODO comments for ambiguous patterns that need human review.
"""


class MigrationAgent(BaseAgent):
    name          = "MigrationAgent"
    deployment    = MODEL_MIGRATION
    system_prompt = SYSTEM_PROMPT
    max_tokens    = MAX_TOKENS_MIGRATION

    def __init__(
        self,
        vector_store: CodeVectorStore,
        reader: LargeFileReader,
        writer: FileWriter,
        output_root: str,
    ):
        super().__init__()
        self._vs          = vector_store
        self._reader      = reader
        self._writer      = writer
        self._output_root = output_root

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def migrate_artifact(
        self,
        artifact: EmberArtifact,
        blueprint: dict,
        module_context: str = "",
    ) -> Dict[str, str]:
        """
        Migrate a single Ember artifact.

        Returns a dict mapping angular_relative_path → migrated_content.
        """
        logger.info("Migrating %s '%s'", artifact.artifact_type, artifact.name)
        self.reset_conversation()

        # Inject relevant context from the vector store
        context = self._vs.search_text(
            f"Migrating {artifact.artifact_type} {artifact.name} to Angular",
            top_k=4,
        )
        if context != "(no relevant context found)":
            self.inject_context(context)

        if module_context:
            self.inject_context(f"[Module context]\n{module_context}")

        # Dispatch to type-specific handler
        handlers = {
            "component":  self._migrate_component,
            "service":    self._migrate_service,
            "model":      self._migrate_model,
            "route":      self._migrate_route,
            "controller": self._migrate_controller,
            "helper":     self._migrate_helper,
            "mixin":      self._migrate_mixin,
            "adapter":    self._migrate_adapter,
            "serializer": self._migrate_serializer,
        }
        handler = handlers.get(artifact.artifact_type, self._migrate_generic)
        migrated_files = handler(artifact, blueprint)

        # Write files to disk
        written = {}
        for rel_path, content in migrated_files.items():
            ok = self._writer.write(rel_path, content, overwrite=True)
            if ok:
                written[rel_path] = content

        logger.info("Migrated %s → %d files written", artifact.name, len(written))
        return written

    def migrate_template(self, artifact: EmberArtifact, blueprint: dict) -> str:
        """
        Dedicated HBS → Angular HTML template pass.
        Returns Angular template string.
        """
        if not artifact.template_path:
            return "<!-- No Ember template found -->"

        scan    = self._reader.read(artifact.template_path)
        content = scan.full_content()

        # If template is huge, process chunk by chunk
        if scan.chunk_count > 1:
            return self._migrate_template_chunked(artifact, scan, blueprint)

        return self._migrate_template_single(artifact, content, blueprint)

    # ─────────────────────────────────────────────────────────────────────────
    # Type-specific migration handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _migrate_component(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal    = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab     = pascal_to_kebab(pascal)
        feat_path = f"src/app/{artifact.name}"

        js_content = self._read_full(artifact.file_path)

        ts_prompt = f"""\
Migrate this Ember component to Angular 17+ TypeScript.

## Ember Component: {artifact.name}
### Migration Blueprint
{json.dumps(blueprint, indent=2)}

### Ember Source ({artifact.file_path})
```javascript
{js_content}
```

### Service Injections
{artifact.service_injections}

### Actions to migrate
{artifact.actions}

### Computed Properties to migrate
{artifact.computed_props}

### Lifecycle Hooks
{artifact.lifecycle_hooks}

Generate the COMPLETE Angular TypeScript component.
- Class name: {pascal}Component
- Preserve ALL business logic exactly
- Use inject() for services
- Use Signals for reactive state
- Add @Input()/@Output() decorators where Ember used attributes/actions
- Return ONLY the TypeScript code, no markdown fences
"""
        ts_code = self.call(ts_prompt)
        ts_code  = _strip_fences(ts_code)

        # Template pass
        html_code = self.migrate_template(artifact, blueprint)

        # SCSS (preserve any ember styles referenced)
        scss_prompt = f"""\
Create Angular SCSS for the {pascal}Component.
Ember component name: {artifact.name}
Only include structural/layout styles; do not invent new styles.
Return ONLY SCSS, no markdown.
"""
        scss_code = self.call(scss_prompt)
        scss_code = _strip_fences(scss_code)

        return {
            f"{feat_path}/{kebab}.component.ts":   ts_code,
            f"{feat_path}/{kebab}.component.html": html_code,
            f"{feat_path}/{kebab}.component.scss": scss_code,
            f"{feat_path}/{kebab}.component.spec.ts": self._generate_spec(pascal, artifact),
        }

    def _migrate_service(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal   = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab    = pascal_to_kebab(pascal)
        content  = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Service to an Angular Injectable service.

## Ember Service: {artifact.name}
### Blueprint
{json.dumps(blueprint, indent=2)}

### Ember Source
```javascript
{content}
```

Requirements:
- @Injectable({{ providedIn: 'root' }})
- Use inject() for dependencies
- Replace ember-data store calls with HttpClient observables
- Replace RSVP.Promise with Observable / Promise
- Preserve ALL business logic, computed properties become getter/signals
- Class name: {pascal}Service
- Return ONLY TypeScript code
"""
        ts_code = _strip_fences(self.call(prompt))
        return {f"src/app/services/{kebab}.service.ts": ts_code}

    def _migrate_model(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)
        content = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Data model to a TypeScript interface + Angular Data Service.

## Ember Model: {artifact.name}
### Blueprint
{json.dumps(blueprint, indent=2)}

### Ember Source
```javascript
{content}
```

### Attributes found
{artifact.model_attrs}

Produce TWO files:

### FILE 1: {kebab}.model.ts
TypeScript interface with:
- All DS.attr() → typed interface properties
- Relationships (belongsTo/hasMany) → typed references
- No 'any' unless truly dynamic

### FILE 2: {kebab}.service.ts
Angular service for CRUD operations:
- inject HttpClient
- Methods: getAll(), getById(id), create(dto), update(id, dto), delete(id)
- Return typed Observables

Return JSON: {{ "model": "...", "service": "..." }}
"""
        result = self.call_json(prompt)
        files  = {}
        if result:
            files[f"src/app/models/{kebab}.model.ts"]   = result.get("model", "")
            files[f"src/app/services/{kebab}.service.ts"] = result.get("service", "")
        else:
            files[f"src/app/models/{kebab}.model.ts"] = (
                f"// TODO: migrate model {artifact.name}\nexport interface {pascal} {{\n  id?: string;\n}}\n"
            )
        return files

    def _migrate_route(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)
        content = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Route to Angular.

## Ember Route: {artifact.name}
### Blueprint
{json.dumps(blueprint, indent=2)}

### Ember Source
```javascript
{content}
```

Produce THREE files:

### FILE 1: {kebab}.component.ts
Angular routed component:
- ngOnInit loads data (from what was model() in Ember)
- beforeModel/afterModel logic → guards or resolvers

### FILE 2: {kebab}.resolver.ts
Angular Resolver (if model() fetches data):
- ResolveFn returning Observable<Data>

### FILE 3: {kebab}.guard.ts
Angular CanActivate guard (if beforeModel had auth/redirect logic)

Return JSON: {{ "component": "...", "resolver": "...", "guard": "..." }}
"""
        result = self.call_json(prompt)
        files  = {}
        if result:
            if result.get("component"):
                files[f"src/app/{artifact.name}/{kebab}.component.ts"] = result["component"]
            if result.get("resolver"):
                files[f"src/app/{artifact.name}/{kebab}.resolver.ts"] = result["resolver"]
            if result.get("guard"):
                files[f"src/app/core/guards/{kebab}.guard.ts"] = result["guard"]
        return files

    def _migrate_controller(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        """Controllers merge into their corresponding routed component."""
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        content = self._read_full(artifact.file_path)

        prompt = f"""\
This Ember Controller will be merged into the Angular {pascal}Component.

## Ember Controller: {artifact.name}
```javascript
{content}
```

Generate ONLY the methods and properties that need to be added to the
Angular {pascal}Component class (not a full component – just the class body).

Preserve:
- queryParams → Angular Router queryParams Observable
- sortBy / pagination logic
- All actions → Angular methods
- All computed properties → getters or signals

Return ONLY the class body content (no class declaration), wrapped in triple backticks.
"""
        merge_code = _strip_fences(self.call(prompt))
        rel_path   = f"src/app/{artifact.name}/{pascal_to_kebab(pascal)}.controller-merge.ts"
        content    = f"// Controller merge for {artifact.name}\n// Add these to the component:\n{merge_code}\n"
        return {rel_path: content}

    def _migrate_helper(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)
        content = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Helper to an Angular Pipe.

## Ember Helper: {artifact.name}
```javascript
{content}
```

Generate a complete Angular Pipe:
- @Pipe({{ name: '{kebab_to_camel(artifact.name.replace("/", "-"))}', standalone: true, pure: true }})
- transform() method with exact same logic
- Proper TypeScript types
- Return ONLY TypeScript code
"""
        ts_code = _strip_fences(self.call(prompt))
        return {f"src/app/shared/pipes/{kebab}.pipe.ts": ts_code}

    def _migrate_mixin(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)
        content = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Mixin to Angular.

## Ember Mixin: {artifact.name}
```javascript
{content}
```

Ember Mixins have no direct Angular equivalent.  Choose the best approach:
  a) Abstract base class (if shared lifecycle/methods)
  b) Injectable utility service (if shared data/state)
  c) Standalone directive (if DOM behavior)

Produce the appropriate Angular TypeScript.
Return ONLY the code.
"""
        ts_code = _strip_fences(self.call(prompt))
        return {f"src/app/shared/mixins/{kebab}.mixin.ts": ts_code}

    def _migrate_adapter(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)
        content = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Data Adapter to an Angular HttpClient Interceptor or base service.

## Ember Adapter: {artifact.name}
```javascript
{content}
```

Map:
- Custom headers → HttpInterceptor
- URL transformations → HttpInterceptor or environment config
- Custom AJAX options → HttpClient options

Return an Angular HTTP interceptor.
Return ONLY the TypeScript code.
"""
        ts_code = _strip_fences(self.call(prompt))
        return {f"src/app/core/interceptors/{kebab}.interceptor.ts": ts_code}

    def _migrate_serializer(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)
        content = self._read_full(artifact.file_path)

        prompt = f"""\
Migrate this Ember Data Serializer to Angular.

## Ember Serializer: {artifact.name}
```javascript
{content}
```

Map:
- normalize() / normalizeResponse() → RxJS map() operator or class transformer
- serialize() → DTO mapper function

Generate a TypeScript mapper class.
Return ONLY the code.
"""
        ts_code = _strip_fences(self.call(prompt))
        return {f"src/app/core/mappers/{kebab}.mapper.ts": ts_code}

    def _migrate_generic(self, artifact: EmberArtifact, blueprint: dict) -> dict[str, str]:
        content = self._read_full(artifact.file_path)
        pascal  = kebab_to_pascal(artifact.name.replace("/", "-"))
        kebab   = pascal_to_kebab(pascal)

        prompt = f"""\
Migrate this Ember artifact of type "{artifact.artifact_type}" to Angular.

## Blueprint
{json.dumps(blueprint, indent=2)}

## Source
```javascript
{content}
```

Generate equivalent Angular TypeScript.
Preserve ALL business logic.
Return ONLY the code.
"""
        ts_code = _strip_fences(self.call(prompt))
        return {f"src/app/misc/{kebab}.ts": ts_code}

    # ─────────────────────────────────────────────────────────────────────────
    # Template migration
    # ─────────────────────────────────────────────────────────────────────────

    def _migrate_template_single(
        self, artifact: EmberArtifact, hbs_content: str, blueprint: dict
    ) -> str:
        pascal = kebab_to_pascal(artifact.name.replace("/", "-"))

        prompt = f"""\
Migrate this Ember Handlebars template to Angular 17+ HTML template.

## Component: {pascal}Component
### Blueprint
{json.dumps(blueprint, indent=2)}

### Ember HBS template
```handlebars
{hbs_content}
```

Migration rules:
  {{{{if cond}}}} block → @if (cond) {{ ... }}
  {{{{unless cond}}}} → @if (!cond) {{ ... }}
  {{{{each items as |item|}}}} → @for (item of items; track item.id) {{ ... }}
  {{{{component-name arg=val}}}} → <app-component-name [input]="val">
  {{{{action "name"}}}} → (click)="name()"
  {{{{model.prop}}}} → {{ model.prop }}
  link-to → routerLink
  input helpers → [formControl] or [(ngModel)]

Return ONLY the Angular HTML template, no markdown.
"""
        html = _strip_fences(self.call(prompt))
        return html

    def _migrate_template_chunked(self, artifact, scan, blueprint) -> str:
        pascal   = kebab_to_pascal(artifact.name.replace("/", "-"))
        parts: list[str] = []
        self.reset_conversation()

        for chunk in scan.chunks:
            prompt = f"""\
Migrating Handlebars template for {pascal}Component – chunk {chunk.chunk_index + 1}/{scan.chunk_count}.
Lines {chunk.start_line}–{chunk.end_line}.

```handlebars
{chunk.content}
```

Convert this portion to Angular HTML template syntax.
Maintain continuity with previous chunks.
Return ONLY the Angular HTML, no markdown.
"""
            html_part = _strip_fences(self.call(prompt))
            parts.append(f"<!-- Lines {chunk.start_line}–{chunk.end_line} -->\n{html_part}")

        return "\n".join(parts)

    # ─────────────────────────────────────────────────────────────────────────
    # Spec generator
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_spec(self, pascal: str, artifact: EmberArtifact) -> str:
        kebab = pascal_to_kebab(pascal)
        prompt = f"""\
Generate a comprehensive Angular unit test spec for {pascal}Component.

Artifact info:
  - Actions: {artifact.actions}
  - Services: {artifact.service_injections}
  - Lifecycle hooks: {artifact.lifecycle_hooks}

Use:
  - Jasmine/Karma syntax
  - TestBed with proper imports
  - Mock services with spyOn
  - Test each action and lifecycle hook

Return ONLY the TypeScript spec code.
"""
        return _strip_fences(self.call(prompt))

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _read_full(self, file_path: str) -> str:
        scan = self._reader.read(file_path)
        if scan.error:
            return f"// File not found: {file_path}"
        return scan.full_content()


def _strip_fences(code: str) -> str:
    """Remove markdown code fences from LLM output."""
    code = code.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        # Remove opening fence (with optional language tag)
        start = 1
        # Remove closing fence
        end = len(lines)
        if lines[-1].strip() == "```":
            end -= 1
        return "\n".join(lines[start:end])
    return code
