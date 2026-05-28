"""
Ember → Angular Migration Agent – CLI Entry Point
─────────────────────────────────────────────────
Usage examples
──────────────
  # Run full migration
  python main.py migrate --ember-path ./my-ember-app --output ./angular-output

  # Check status of a running/completed migration
  python main.py status

  # Reset all failed artifacts and retry
  python main.py retry-failed

  # Export a detailed JSON report
  python main.py report --out migration_report.json

  # Dry-run (analyse without writing files)
  python main.py migrate --ember-path ./my-ember-app --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO", log_dir: str = "logs"):
    Path(log_dir).mkdir(exist_ok=True)
    log_file = Path(log_dir) / "migration.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, handlers=handlers)

    # Silence noisy libraries
    for lib in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def cmd_migrate(args):
    """Run the full migration pipeline."""
    from ember_to_angular.orchestrator import OrchestratorAgent
    from ember_to_angular.config.settings import STATE_FILE, VECTOR_STORE_PATH

    ember_path   = args.ember_path
    angular_path = args.output or "angular_output"
    dry_run      = args.dry_run

    if not Path(ember_path).exists():
        print(f"[ERROR] Ember project path does not exist: {ember_path}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Ember → Angular Multi-Agent Migration")
    print(f"  Source : {ember_path}")
    print(f"  Output : {angular_path}")
    print(f"  Dry-run: {dry_run}")
    print(f"{'='*60}\n")

    orchestrator = OrchestratorAgent(
        ember_path         = ember_path,
        angular_path       = angular_path,
        state_file         = STATE_FILE,
        vector_store_path  = VECTOR_STORE_PATH,
        dry_run            = dry_run,
    )

    report = orchestrator.run()

    print(f"\n{'='*60}")
    print("MIGRATION SUMMARY")
    print(f"{'='*60}")
    for k, v in report.items():
        print(f"  {k:<25}: {v}")
    print(f"{'='*60}\n")

    if report.get("failed"):
        print(f"[WARN] {report['failed']} artifact(s) failed.  "
              f"Run `python main.py retry-failed` to retry.")
    else:
        print("[SUCCESS] Migration complete.")


def cmd_status(args):
    """Show current migration progress."""
    from ember_to_angular.orchestrator import StateManager
    from ember_to_angular.config.settings import STATE_FILE

    sm = StateManager(STATE_FILE)
    if not sm.exists():
        print("No migration in progress.  Run `python main.py migrate` first.")
        return

    print(sm.detailed_report())


def cmd_retry_failed(args):
    """Reset all failed artifacts to PENDING and re-run migration."""
    from ember_to_angular.orchestrator import StateManager, OrchestratorAgent
    from ember_to_angular.config.settings import STATE_FILE, VECTOR_STORE_PATH

    sm = StateManager(STATE_FILE)
    if not sm.exists():
        print("No migration state found.")
        return

    state  = sm.load()
    failed = sm.reset_all_failed()
    print(f"Reset {len(failed)} failed artifacts: {failed}")

    orchestrator = OrchestratorAgent(
        ember_path        = state.ember_project_path,
        angular_path      = state.angular_output_path,
        state_file        = STATE_FILE,
        vector_store_path = VECTOR_STORE_PATH,
    )
    orchestrator.run()


def cmd_report(args):
    """Export a detailed JSON migration report."""
    from ember_to_angular.orchestrator import StateManager
    from ember_to_angular.config.settings import STATE_FILE

    out = args.out or "migration_report.json"
    sm  = StateManager(STATE_FILE)
    sm.export_report(out)
    print(f"Report exported to {out}")


def cmd_analyze_only(args):
    """Analyse the Ember project without migrating (useful for planning)."""
    from ember_to_angular.tools.ember_parser import EmberParser
    from ember_to_angular.tools.file_reader import LargeFileReader
    from ember_to_angular.memory.vector_store import CodeVectorStore
    from ember_to_angular.agents.analyzer_agent import AnalyzerAgent
    from ember_to_angular.config.settings import VECTOR_STORE_PATH

    ember_path = args.ember_path
    if not Path(ember_path).exists():
        print(f"[ERROR] Path not found: {ember_path}")
        sys.exit(1)

    reader  = LargeFileReader()
    parser  = EmberParser(reader)
    vs      = CodeVectorStore(VECTOR_STORE_PATH)
    agent   = AnalyzerAgent(vs, reader)

    print("Parsing Ember project …")
    project = parser.parse(ember_path)
    print(project.summary())

    print("\nRunning project analysis …")
    analysis = agent.analyze_project(project)
    print("\n=== Analysis Result ===")
    import json
    print(json.dumps(analysis, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ember-to-angular",
        description="Multi-agent Ember.js → Angular 17 migration system",
    )
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-dir", default="logs")

    sub = parser.add_subparsers(dest="command", required=True)

    # migrate
    p_migrate = sub.add_parser("migrate", help="Run the full migration pipeline")
    p_migrate.add_argument("--ember-path", required=True,
                           help="Path to the Ember.js project root")
    p_migrate.add_argument("--output", default=None,
                           help="Angular output directory (default: angular_output)")
    p_migrate.add_argument("--dry-run", action="store_true",
                           help="Analyse and plan without writing files")
    p_migrate.set_defaults(func=cmd_migrate)

    # status
    p_status = sub.add_parser("status", help="Show migration progress")
    p_status.set_defaults(func=cmd_status)

    # retry-failed
    p_retry = sub.add_parser("retry-failed", help="Retry failed artifacts")
    p_retry.set_defaults(func=cmd_retry_failed)

    # report
    p_report = sub.add_parser("report", help="Export migration report")
    p_report.add_argument("--out", default="migration_report.json")
    p_report.set_defaults(func=cmd_report)

    # analyze-only
    p_analyze = sub.add_parser("analyze", help="Analyse Ember project without migrating")
    p_analyze.add_argument("--ember-path", required=True)
    p_analyze.set_defaults(func=cmd_analyze_only)

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()
    setup_logging(args.log_level, args.log_dir)
    args.func(args)


if __name__ == "__main__":
    main()
