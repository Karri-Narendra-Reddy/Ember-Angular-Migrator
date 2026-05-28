"""
File writer – safely writes Angular migration output to disk.

Features
────────
• Atomic writes  (write to .tmp then rename)
• Backup before overwrite (keeps .bak copies)
• Dry-run mode   (logs what would be written without touching disk)
• Manifest tracking (records every written file for audit trail)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class FileWriter:
    def __init__(
        self,
        output_root: str,
        dry_run: bool = False,
        backup: bool = True,
        manifest_path: str | None = None,
    ):
        self.output_root   = Path(output_root)
        self.dry_run       = dry_run
        self.backup        = backup
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self._manifest: list[dict] = []

        if not dry_run:
            self.output_root.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────────────

    def write(
        self,
        relative_path: str,
        content: str,
        overwrite: bool = True,
        encoding: str = "utf-8",
    ) -> bool:
        """
        Write content to output_root / relative_path.

        Returns True if the file was actually written, False if skipped.
        """
        dest = self.output_root / relative_path

        if dest.exists() and not overwrite:
            logger.debug("Skipped (exists, no overwrite): %s", dest)
            return False

        if self.dry_run:
            logger.info("[DRY-RUN] Would write: %s (%d chars)", dest, len(content))
            return True

        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and self.backup:
            self._backup(dest)

        # Atomic write via temp file
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding=encoding)
            shutil.move(str(tmp), str(dest))
        except Exception as e:
            logger.error("Failed to write %s: %s", dest, e)
            if tmp.exists():
                tmp.unlink()
            return False

        self._record(relative_path, content, dest)
        logger.info("Written: %s", dest)
        return True

    def write_many(self, files: dict[str, str], overwrite: bool = True) -> dict[str, bool]:
        """Write multiple files.  Keys are relative paths, values are content."""
        results = {}
        for rel_path, content in files.items():
            results[rel_path] = self.write(rel_path, content, overwrite=overwrite)
        return results

    def ensure_dir(self, relative_dir: str):
        """Create directory (and parents) under output root."""
        d = self.output_root / relative_dir
        if not self.dry_run:
            d.mkdir(parents=True, exist_ok=True)

    def flush_manifest(self):
        """Write the manifest JSON to disk."""
        if self.manifest_path and self._manifest:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with self.manifest_path.open("w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2)

    # ── Internals ───────────────────────────────────────────────────────────────

    def _backup(self, dest: Path):
        bak = dest.with_suffix(dest.suffix + ".bak")
        shutil.copy2(str(dest), str(bak))
        logger.debug("Backed up %s → %s", dest.name, bak.name)

    def _record(self, relative_path: str, content: str, dest: Path):
        self._manifest.append({
            "path":      relative_path,
            "abs_path":  str(dest),
            "sha256":    hashlib.sha256(content.encode()).hexdigest(),
            "size":      len(content),
            "timestamp": datetime.utcnow().isoformat(),
        })
