import os
from dotenv import load_dotenv

load_dotenv()

# ── Azure APIM / OpenAI Connection ────────────────────────────────────────────
APIM_BASE_URL   = os.getenv("APIM_BASE_URL", "https://pre4apim.azure-api.net")
APIM_KEY        = os.getenv("APIM_KEY", "<API Key>")
APIM_VERSION    = "2024-02-01"
O4_MINI_VERSION = "2024-12-01-preview"
ENDPOINT        = "llmrouting-exp"

# ── Model Routing per Agent Role ──────────────────────────────────────────────
# Orchestrator uses reasoning model; migration uses best code model
MODEL_ORCHESTRATOR = os.getenv("MODEL_ORCHESTRATOR", "o4-mini")
MODEL_ANALYZER     = os.getenv("MODEL_ANALYZER",     "gpt-4.1")
MODEL_STRUCTURE    = os.getenv("MODEL_STRUCTURE",    "gpt-4.1")
MODEL_MIGRATION    = os.getenv("MODEL_MIGRATION",    "gpt-5")
MODEL_VALIDATOR    = os.getenv("MODEL_VALIDATOR",    "gpt-4.1-mini")
MODEL_EMBEDDING    = os.getenv("MODEL_EMBEDDING",    "text-embedding-3-large")

# ── Token Budgets ─────────────────────────────────────────────────────────────
MAX_TOKENS_ORCHESTRATOR = int(os.getenv("MAX_TOKENS_ORCHESTRATOR", 8000))
MAX_TOKENS_ANALYZER     = int(os.getenv("MAX_TOKENS_ANALYZER",     16000))
MAX_TOKENS_MIGRATION    = int(os.getenv("MAX_TOKENS_MIGRATION",    32000))
MAX_TOKENS_VALIDATOR    = int(os.getenv("MAX_TOKENS_VALIDATOR",    8000))

# ── File Reading ───────────────────────────────────────────────────────────────
# How many lines per chunk when reading large files (>20k lines)
CHUNK_SIZE_LINES   = int(os.getenv("CHUNK_SIZE_LINES", 500))
# Overlap lines between chunks to preserve context continuity
CHUNK_OVERLAP      = int(os.getenv("CHUNK_OVERLAP", 50))
# Hard maximum lines read per file scan pass (≥20 000 as requested)
MAX_LINES_PER_FILE = int(os.getenv("MAX_LINES_PER_FILE", 20000))

# ── Paths ─────────────────────────────────────────────────────────────────────
EMBER_PROJECT_PATH   = os.getenv("EMBER_PROJECT_PATH",   "")
ANGULAR_OUTPUT_PATH  = os.getenv("ANGULAR_OUTPUT_PATH",  "angular_output")
STATE_FILE           = os.getenv("STATE_FILE",            "migration_state.json")
VECTOR_STORE_PATH    = os.getenv("VECTOR_STORE_PATH",     "vector_store")
LOGS_DIR             = os.getenv("LOGS_DIR",              "logs")

# ── Ember Patterns ────────────────────────────────────────────────────────────
EMBER_COMPONENT_EXTS  = [".hbs", ".js", ".ts"]
EMBER_IGNORE_DIRS     = ["node_modules", ".git", "dist", "tmp", ".cache"]
EMBER_FILE_PATTERNS   = {
    "route":     ["app/routes/**"],
    "component": ["app/components/**"],
    "service":   ["app/services/**"],
    "model":     ["app/models/**"],
    "adapter":   ["app/adapters/**"],
    "serializer":["app/serializers/**"],
    "controller":["app/controllers/**"],
    "helper":    ["app/helpers/**"],
    "mixin":     ["app/mixins/**"],
    "template":  ["app/templates/**"],
    "config":    ["config/**"],
}

# ── Angular Scaffold ──────────────────────────────────────────────────────────
ANGULAR_STRICT_MODE = True
ANGULAR_STANDALONE  = True   # Use standalone components (Angular 17+)
