"""
Angular code generation helpers.

These are NOT AI-generated; they provide deterministic scaffolding that the
Migration Agent fills with LLM-generated business logic.

Covers Angular 17+ standalone component style.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ember_to_angular.config.settings import ANGULAR_STANDALONE


# ─────────────────────────────────────────────────────────────────────────────
# Naming utilities
# ─────────────────────────────────────────────────────────────────────────────

def kebab_to_pascal(name: str) -> str:
    """'my-component' → 'MyComponent'"""
    return "".join(part.capitalize() for part in re.split(r"[-_/]", name))

def kebab_to_camel(name: str) -> str:
    """'my-service' → 'myService'"""
    parts = re.split(r"[-_/]", name)
    return parts[0] + "".join(p.capitalize() for p in parts[1:])

def pascal_to_kebab(name: str) -> str:
    """'MyComponent' → 'my-component'"""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s1).lower()

def module_path(module_name: str) -> str:
    """Convert ember module name to angular directory path."""
    return module_name.replace("/", os.sep)


# ─────────────────────────────────────────────────────────────────────────────
# Angular scaffold templates
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AngularFile:
    relative_path: str       # relative to angular output root, e.g. src/app/users/users.component.ts
    content: str
    description: str = ""


class AngularScaffold:
    """
    Generates the deterministic structural skeleton for Angular artifacts.
    The LLM fills in business logic; this class provides the wiring.
    """

    def __init__(self, output_root: str):
        self.output_root = Path(output_root)

    # ── Component ──────────────────────────────────────────────────────────────

    def component_ts(
        self,
        name: str,
        selector: str | None = None,
        inputs: list[str] | None = None,
        outputs: list[str] | None = None,
        injected_services: list[str] | None = None,
        body: str = "// TODO: migrate business logic from Ember component",
    ) -> AngularFile:
        pascal  = kebab_to_pascal(name)
        sel     = selector or pascal_to_kebab(pascal)
        ins     = inputs or []
        outs    = outputs or []
        svcs    = injected_services or []

        svc_imports = "\n".join(
            f"import {{ {kebab_to_pascal(s)}Service }} from '../services/{s}.service';"
            for s in svcs
        )
        svc_injections = "\n  ".join(
            f"private {kebab_to_camel(s)}Service = inject({kebab_to_pascal(s)}Service);"
            for s in svcs
        )
        input_defs  = "\n  ".join(f"@Input() {i}: any;" for i in ins)
        output_defs = "\n  ".join(f"@Output() {o} = new EventEmitter<any>();" for o in outs)

        standalone_decorator = "standalone: true," if ANGULAR_STANDALONE else ""

        content = f"""\
import {{ Component, OnInit, OnDestroy, Input, Output, EventEmitter, inject }} from '@angular/core';
import {{ CommonModule }} from '@angular/common';
import {{ ReactiveFormsModule }} from '@angular/forms';
{svc_imports}

@Component({{
  {standalone_decorator}
  selector: '{sel}',
  templateUrl: './{pascal_to_kebab(pascal)}.component.html',
  styleUrls: ['./{pascal_to_kebab(pascal)}.component.scss'],
  imports: [CommonModule, ReactiveFormsModule],
}})
export class {pascal}Component implements OnInit, OnDestroy {{
  {input_defs}
  {output_defs}
  {svc_injections}

  ngOnInit(): void {{
    // migrated from Ember init / didInsertElement
  }}

  ngOnDestroy(): void {{
    // migrated from Ember willDestroyElement / willDestroy
  }}

  {body}
}}
"""
        rel_path = f"src/app/{name}/{pascal_to_kebab(pascal)}.component.ts"
        return AngularFile(relative_path=rel_path, content=content, description=f"Component: {pascal}")

    def component_html(self, name: str, body: str = "<!-- TODO: migrate Ember template -->") -> AngularFile:
        pascal = kebab_to_pascal(name)
        rel_path = f"src/app/{name}/{pascal_to_kebab(pascal)}.component.html"
        content = f"<!-- {pascal} Component -->\n{body}\n"
        return AngularFile(relative_path=rel_path, content=content, description=f"Template: {pascal}")

    def component_scss(self, name: str, body: str = "/* TODO: migrate Ember styles */") -> AngularFile:
        pascal = kebab_to_pascal(name)
        rel_path = f"src/app/{name}/{pascal_to_kebab(pascal)}.component.scss"
        return AngularFile(relative_path=rel_path, content=body + "\n", description=f"Styles: {pascal}")

    def component_spec(self, name: str) -> AngularFile:
        pascal = kebab_to_pascal(name)
        kebab  = pascal_to_kebab(pascal)
        rel_path = f"src/app/{name}/{kebab}.component.spec.ts"
        content = f"""\
import {{ ComponentFixture, TestBed }} from '@angular/core/testing';
import {{ {pascal}Component }} from './{kebab}.component';

describe('{pascal}Component', () => {{
  let component: {pascal}Component;
  let fixture: ComponentFixture<{pascal}Component>;

  beforeEach(async () => {{
    await TestBed.configureTestingModule({{
      imports: [{pascal}Component],
    }}).compileComponents();

    fixture = TestBed.createComponent({pascal}Component);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }});

  it('should create', () => {{
    expect(component).toBeTruthy();
  }});
}});
"""
        return AngularFile(relative_path=rel_path, content=content, description=f"Spec: {pascal}")

    # ── Service ────────────────────────────────────────────────────────────────

    def service_ts(
        self,
        name: str,
        injected_services: list[str] | None = None,
        body: str = "// TODO: migrate Ember service logic",
    ) -> AngularFile:
        pascal = kebab_to_pascal(name)
        svcs   = injected_services or []
        svc_imports = "\n".join(
            f"import {{ {kebab_to_pascal(s)}Service }} from './{s}.service';"
            for s in svcs
        )
        svc_injections = "\n  ".join(
            f"private {kebab_to_camel(s)}Service = inject({kebab_to_pascal(s)}Service);"
            for s in svcs
        )
        content = f"""\
import {{ Injectable, inject }} from '@angular/core';
import {{ HttpClient }} from '@angular/common/http';
import {{ Observable }} from 'rxjs';
{svc_imports}

@Injectable({{
  providedIn: 'root',
}})
export class {pascal}Service {{
  private http = inject(HttpClient);
  {svc_injections}

  {body}
}}
"""
        rel_path = f"src/app/services/{pascal_to_kebab(pascal)}.service.ts"
        return AngularFile(relative_path=rel_path, content=content, description=f"Service: {pascal}")

    # ── Model / Interface ──────────────────────────────────────────────────────

    def model_ts(self, name: str, attributes: list[str] | None = None) -> AngularFile:
        pascal = kebab_to_pascal(name)
        attrs  = attributes or []
        attr_lines = "\n  ".join(f"{a}?: any;" for a in attrs) or "// TODO: define model properties"
        content = f"""\
/**
 * {pascal} model – migrated from Ember Data model.
 * Replace 'any' types with proper TypeScript types.
 */
export interface {pascal} {{
  id?: string | number;
  {attr_lines}
}}
"""
        rel_path = f"src/app/models/{pascal_to_kebab(pascal)}.model.ts"
        return AngularFile(relative_path=rel_path, content=content, description=f"Model: {pascal}")

    # ── Route ──────────────────────────────────────────────────────────────────

    def route_module(self, name: str, children: list[str] | None = None) -> AngularFile:
        pascal = kebab_to_pascal(name)
        child_routes = ""
        if children:
            child_routes = "\n".join(
                f"  {{ path: '{c}', loadComponent: () => import('../{c}/{pascal_to_kebab(kebab_to_pascal(c))}.component').then(m => m.{kebab_to_pascal(c)}Component) }},"
                for c in children
            )
        content = f"""\
import {{ Routes }} from '@angular/router';

export const {pascal_to_kebab(pascal).replace('-', '_').upper()}_ROUTES: Routes = [
  {{
    path: '',
    loadComponent: () =>
      import('./{pascal_to_kebab(pascal)}.component').then(m => m.{pascal}Component),
    children: [
{child_routes}
    ],
  }},
];
"""
        rel_path = f"src/app/{name}/{pascal_to_kebab(pascal)}.routes.ts"
        return AngularFile(relative_path=rel_path, content=content, description=f"Routes: {pascal}")

    # ── Module barrel ──────────────────────────────────────────────────────────

    def barrel_index(self, name: str, exports: list[str]) -> AngularFile:
        lines = [f"export * from './{e}';" for e in exports]
        rel_path = f"src/app/{name}/index.ts"
        return AngularFile(relative_path=rel_path, content="\n".join(lines) + "\n", description=f"Barrel: {name}")

    # ── Write helper ───────────────────────────────────────────────────────────

    def write(self, angular_file: AngularFile, overwrite: bool = False):
        dest = self.output_root / angular_file.relative_path
        if dest.exists() and not overwrite:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(angular_file.content, encoding="utf-8")

    def write_many(self, files: list[AngularFile], overwrite: bool = False):
        for f in files:
            self.write(f, overwrite=overwrite)


# ─────────────────────────────────────────────────────────────────────────────
# Angular project-level files
# ─────────────────────────────────────────────────────────────────────────────

def generate_app_routes(route_tree: dict[str, list]) -> str:
    """Generate top-level app.routes.ts from the parsed Ember route tree."""
    route_entries = []
    for route_name, children in route_tree.items():
        pascal = kebab_to_pascal(route_name)
        entry = f"""\
  {{
    path: '{route_name}',
    loadChildren: () => import('./{route_name}/{pascal_to_kebab(pascal)}.routes').then(m => m.{pascal.upper()}_ROUTES),
  }},"""
        route_entries.append(entry)

    return f"""\
import {{ Routes }} from '@angular/router';

export const APP_ROUTES: Routes = [
  {{ path: '', redirectTo: 'index', pathMatch: 'full' }},
{chr(10).join(route_entries)}
  {{ path: '**', redirectTo: 'index' }},
];
"""

def generate_app_config() -> str:
    return """\
import { ApplicationConfig } from '@angular/core';
import { provideRouter, withHashLocation } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { APP_ROUTES } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideRouter(APP_ROUTES, withHashLocation()),
    provideHttpClient(),
  ],
};
"""

def generate_app_component() -> str:
    return """\
import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';

@Component({
  standalone: true,
  selector: 'app-root',
  template: `<router-outlet />`,
  imports: [RouterOutlet],
})
export class AppComponent {}
"""

def generate_main_ts() -> str:
    return """\
import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { appConfig } from './app/app.config';

bootstrapApplication(AppComponent, appConfig)
  .catch(err => console.error(err));
"""

def generate_tsconfig() -> str:
    return """\
{
  "compileOnSave": false,
  "compilerOptions": {
    "baseUrl": "./",
    "outDir": "./dist/out-tsc",
    "strict": true,
    "noImplicitOverride": true,
    "noPropertyAccessFromIndexSignature": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "esModuleInterop": true,
    "experimentalDecorators": true,
    "moduleResolution": "bundler",
    "importHelpers": true,
    "target": "ES2022",
    "module": "ES2022",
    "useDefineForClassFields": false,
    "lib": ["ES2022", "dom"],
    "paths": {
      "@app/*": ["src/app/*"],
      "@shared/*": ["src/app/shared/*"],
      "@services/*": ["src/app/services/*"],
      "@models/*": ["src/app/models/*"]
    }
  },
  "angularCompilerOptions": {
    "enableI18nLegacyMessageIdFormat": false,
    "strictInjectionParameters": true,
    "strictInputAccessModifiers": true,
    "strictTemplates": true
  }
}
"""

def generate_package_json(project_name: str) -> str:
    return f"""\
{{
  "name": "{project_name}-angular",
  "version": "0.0.0",
  "scripts": {{
    "ng": "ng",
    "start": "ng serve",
    "build": "ng build",
    "watch": "ng build --watch --configuration development",
    "test": "ng test"
  }},
  "dependencies": {{
    "@angular/animations": "^17.3.0",
    "@angular/common": "^17.3.0",
    "@angular/compiler": "^17.3.0",
    "@angular/core": "^17.3.0",
    "@angular/forms": "^17.3.0",
    "@angular/platform-browser": "^17.3.0",
    "@angular/platform-browser-dynamic": "^17.3.0",
    "@angular/router": "^17.3.0",
    "rxjs": "~7.8.0",
    "tslib": "^2.6.0",
    "zone.js": "~0.14.0"
  }},
  "devDependencies": {{
    "@angular-devkit/build-angular": "^17.3.0",
    "@angular/cli": "^17.3.0",
    "@angular/compiler-cli": "^17.3.0",
    "@types/jasmine": "~5.1.0",
    "jasmine-core": "~5.1.0",
    "karma": "~6.4.0",
    "karma-chrome-launcher": "~3.2.0",
    "karma-coverage": "~2.2.0",
    "karma-jasmine": "~5.1.0",
    "karma-jasmine-html-reporter": "~2.1.0",
    "typescript": "~5.4.0"
  }}
}}
"""
