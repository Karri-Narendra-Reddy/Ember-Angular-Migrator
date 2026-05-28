# Ember → Angular Multi-Agent Migration System

A production-grade multi-agent orchestration system that automatically migrates complex Ember.js applications to Angular 17+ standalone architecture using Azure OpenAI through APIM.

---

## Quick Start

### Step 1 — Clone / open the project

```bash
cd Ember-angular
```

### Step 2 — Create a virtual environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure environment variables

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Open `.env` and fill in:

```env
APIM_BASE_URL=https://pre4apim.azure-api.net   # your APIM base URL
APIM_KEY=<your-api-key-here>                    # your APIM subscription key
EMBER_PROJECT_PATH=./path/to/your-ember-app     # optional default path
ANGULAR_OUTPUT_PATH=./angular_output            # where Angular files are written
```

### Step 5 — Run the migration

```bash
python main.py migrate --ember-path ./path/to/your-ember-app --output ./angular-output
```

That's it. The agents will run end-to-end and write the Angular project to `./angular-output`.

---

## All Commands

| Command | What it does |
|---|---|
| `python main.py migrate --ember-path <path>` | Run the full migration pipeline |
| `python main.py migrate --ember-path <path> --dry-run` | Analyse + plan without writing any files |
| `python main.py analyze --ember-path <path>` | Analyse the Ember project only (no migration) |
| `python main.py status` | Show live migration progress |
| `python main.py retry-failed` | Reset failed artifacts and resume |
| `python main.py report --out report.json` | Export a full JSON report |

### Full flag reference

```bash
python main.py migrate \
  --ember-path  ./my-ember-app   \   # path to Ember project root (required)
  --output      ./angular-output \   # Angular output directory   (default: angular_output)
  --dry-run                          # analyse only, no files written

python main.py report \
  --out migration_report.json        # output file path           (default: migration_report.json)

python main.py --log-level DEBUG     # set log verbosity: DEBUG | INFO | WARNING | ERROR
python main.py --log-dir ./my-logs   # custom log directory       (default: logs/)
```

---

## Resuming an Interrupted Migration

The migration is **fully resumable**. If you interrupt it (`Ctrl+C`) or it crashes, just re-run the same command:

```bash
python main.py migrate --ember-path ./my-ember-app --output ./angular-output
```

The tracker reads `migration_state.json` and picks up exactly where it left off — already-validated artifacts are skipped automatically.

To retry all failed artifacts:

```bash
python main.py retry-failed
```

---

## Architecture

The workflow is a **LangGraph StateGraph** — each processing step is a node, routing decisions are conditional edges. All agents are pure Python classes injected into the graph via closures.

```
LangGraph StateGraph  (CompiledStateGraph)
──────────────────────────────────────────────────────────────────────────────
START
  → scan                 parse Ember project + index all files into vector store
  → analyze_project      gpt-4.1   : project overview & business domains
  → decide_order         o4-mini   : optimal module migration sequence (reasoning)
  → create_structure     gpt-4.1   : design + write Angular skeleton files
  → pick_module ──────────────────────────────────────────────────────────────┐
  → analyze_module       gpt-4.1   : module-level plan                         │
  → pick_artifact ─────────────────────────────────────────────────────────┐   │
  → analyze_artifact     gpt-4.1   : deep chunk-by-chunk blueprint (20k+ lines)│   │
  → migrate_artifact     gpt-5     : generate Angular TS, HTML, SCSS, spec  │   │
  → validate             gpt-4.1-mini : cross-check vs Ember source         │   │
       ├─ passed ─────────────────────────────────────────────────────────── ┘   │
       ├─ needs_fix → fix_artifact → validate  (up to 2 retries)                 │
       └─ failed  ─────────────────────────────────────────────────────────  ┘   │
  → (no more artifacts) ─────────────────────────────────────────────────────── ┘
  → (no more modules)  → finalize → END
──────────────────────────────────────────────────────────────────────────────

Supporting Infrastructure
  LargeFileReader    – chunked reading with 50-line overlap (handles 20k+ line files)
  EmberParser        – static analysis  (routes, components, services, models…)
  CodeVectorStore    – text-embedding-3-large semantic context retrieval
  MigrationTracker   – durable JSON state machine (survives interrupts / crashes)
  FileWriter         – atomic writes with .bak backup & migration manifest
  AngularScaffold    – deterministic Angular 17 boilerplate generator
```

### Agent Roles

| Agent | Model | Role |
|---|---|---|
| **OrchestratorAgent** | o4-mini | Wires agents, compiles & runs the LangGraph |
| **AnalyzerAgent** | gpt-4.1 | Deep-reads Ember files chunk-by-chunk; extracts every business rule |
| **StructureAgent** | gpt-4.1 | Designs Angular project structure aligned with discovered business domains |
| **MigrationAgent** | gpt-5 | Generates complete Angular TypeScript, HTML templates, SCSS, and specs |
| **ValidatorAgent** | gpt-4.1-mini | Reviews migrated code against Ember source; auto-fixes errors (up to 2×) |

---

## Project File Structure

```
Ember-angular/
├── .env                          ← your credentials (never commit this)
├── .env.example                  ← template to copy from
├── .gitignore
├── knowledge.py                  ← LLM connection reference / examples
├── main.py                       ← CLI entry point
├── requirements.txt
├── README.md
│
└── ember_to_angular/
    ├── config/
    │   └── settings.py           ← all env vars & defaults
    │
    ├── agents/
    │   ├── base_agent.py         ← LLM call wrapper (http + langchain fallback)
    │   ├── analyzer_agent.py     ← deep Ember file reader & blueprint producer
    │   ├── structure_agent.py    ← Angular scaffold designer
    │   ├── migration_agent.py    ← per-artifact Angular code generator
    │   └── validator_agent.py    ← migrated code reviewer + auto-fixer
    │
    ├── orchestrator/
    │   ├── migration_graph.py    ← LangGraph StateGraph (12 nodes, conditional edges)
    │   ├── orchestrator_agent.py ← wires agents + runs the graph
    │   └── state_manager.py      ← report / reset / export helpers
    │
    ├── tools/
    │   ├── llm_client.py         ← raw APIM HTTP + LangChain helpers
    │   ├── file_reader.py        ← chunked reader (20k+ line support)
    │   ├── ember_parser.py       ← static Ember project analyser
    │   ├── angular_generator.py  ← Angular 17 boilerplate templates
    │   └── file_writer.py        ← atomic file writer with backup + manifest
    │
    └── memory/
        ├── vector_store.py       ← embedding-based semantic search
        └── migration_tracker.py  ← per-artifact state machine (JSON persistence)
```

---

## What Gets Migrated

| Ember Artifact | Angular Equivalent |
|---|---|
| Route + `model()` | Routed Component + `ResolveFn` Resolver |
| `beforeModel` / `afterModel` | `CanActivateFn` Guard |
| Component (`.js` + `.hbs`) | Standalone Component (`.ts` + `.html` + `.scss` + `.spec.ts`) |
| Service | `@Injectable({ providedIn: 'root' })` Service |
| Ember Data Model | TypeScript Interface + HttpClient Service |
| Adapter | `HttpInterceptorFn` |
| Serializer | Mapper class |
| Helper | Angular `Pipe` |
| Mixin | Abstract base class or Injectable utility service |
| Controller | Merged into its routed Component |

---

## Environment Variables

All variables are optional except `APIM_KEY`.

| Variable | Default | Description |
|---|---|---|
| `APIM_BASE_URL` | `https://pre4apim.azure-api.net` | Azure APIM base URL |
| `APIM_KEY` | *(required)* | Your APIM subscription key |
| `MODEL_ORCHESTRATOR` | `o4-mini` | Reasoning model for migration planning |
| `MODEL_ANALYZER` | `gpt-4.1` | Model for Ember project analysis |
| `MODEL_STRUCTURE` | `gpt-4.1` | Model for Angular structure design |
| `MODEL_MIGRATION` | `gpt-5` | Model for Angular code generation |
| `MODEL_VALIDATOR` | `gpt-4.1-mini` | Model for validation + auto-fix |
| `MODEL_EMBEDDING` | `text-embedding-3-large` | Embedding model for context retrieval |
| `EMBER_PROJECT_PATH` | *(none)* | Default Ember path (overridden by `--ember-path`) |
| `ANGULAR_OUTPUT_PATH` | `angular_output` | Angular output directory |
| `STATE_FILE` | `migration_state.json` | Migration state persistence file |
| `VECTOR_STORE_PATH` | `vector_store` | Directory for the embedding store |
| `LOGS_DIR` | `logs` | Log file directory |
| `MAX_LINES_PER_FILE` | `20000` | Max lines read per file scan pass |
| `CHUNK_SIZE_LINES` | `500` | Lines per LLM chunk |
| `CHUNK_OVERLAP` | `50` | Overlap lines between chunks (preserves context) |
| `MAX_TOKENS_MIGRATION` | `32000` | Token budget for migration agent |
| `MAX_TOKENS_ANALYZER` | `16000` | Token budget for analyzer agent |

---

## Angular Output Structure

```
angular_output/
├── package.json
├── tsconfig.json
├── .gitignore
├── README.md
├── migration_manifest.json       ← every written file with hash + timestamp
├── src/
│   ├── main.ts
│   ├── styles.scss
│   ├── environments/
│   │   ├── environment.ts
│   │   └── environment.prod.ts
│   └── app/
│       ├── app.component.ts
│       ├── app.config.ts
│       ├── app.routes.ts
│       ├── core/
│       │   ├── interceptors/    ← migrated from Ember adapters
│       │   ├── guards/          ← migrated from beforeModel / afterModel
│       │   └── mappers/         ← migrated from Ember serializers
│       ├── shared/
│       │   ├── components/      ← shared UI components
│       │   └── pipes/           ← migrated from Ember helpers
│       ├── models/              ← TypeScript interfaces from Ember Data models
│       ├── services/            ← migrated from Ember services
│       └── <feature>/           ← one directory per Ember route/module
│           ├── <name>.component.ts
│           ├── <name>.component.html
│           ├── <name>.component.scss
│           ├── <name>.component.spec.ts
│           └── <name>.routes.ts
```

---

## .gitignore

Add this to your `.gitignore` at the root of this project:

```gitignore
# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd
.Python
*.egg
*.egg-info/
dist/
build/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.so
.installed.cfg
*.egg-link

# Virtual environments
.venv/
venv/
env/
ENV/
.env.bak

# Credentials — NEVER commit these
.env
*.key
*.pem
secrets.json

# Migration runtime artifacts
migration_state.json
migration_report.json
vector_store/
logs/
angular_output/

# Backup files created by FileWriter
*.bak
*.tmp

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~
.DS_Store
Thumbs.db

# Angular build output (if you run ng build inside angular_output)
angular_output/dist/
angular_output/.angular/
angular_output/node_modules/
```

---

## Key Features

- **20 000+ lines per file** — the chunked reader processes arbitrarily large files with 50-line overlap between chunks so no context is lost at boundaries
- **LangGraph workflow** — the full pipeline is a compiled `StateGraph`; every step is a node, routing is conditional edges — easy to extend or visualise
- **Resumable** — migration state is saved to `migration_state.json` after every artifact; safe to `Ctrl+C` and restart at any time
- **Semantic context retrieval** — `text-embedding-3-large` indexes all source code; agents retrieve only the most relevant chunks per call instead of re-reading everything
- **Business logic preservation** — ValidatorAgent cross-checks every migrated file against the Ember source and flags any missing logic
- **Auto-fix loop** — up to 2 automatic fix attempts per artifact before escalating to human review
- **Dependency-aware ordering** — o4-mini reasons about the correct order: models → services → shared → routes → components; parent routes before children
- **Atomic writes** — every file is written via a `.tmp` → rename pattern; `.bak` backups are kept; a `migration_manifest.json` records every written file with SHA-256 hash
