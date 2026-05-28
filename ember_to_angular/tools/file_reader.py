"""
Large-file reader capable of handling files with 20 000+ lines.

Key design decisions
────────────────────
• Files are read in overlapping chunks so no context is lost at boundaries.
• A ScanResult captures every chunk along with metadata (file, chunk_index,
  start_line, end_line, total_lines).
• read_full() collapses all chunks into a single string with positional headers,
  useful when the LLM context window is large enough.
• iter_chunks() yields one chunk at a time for streaming / token-budget-aware
  agents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, List

from ember_to_angular.config.settings import (
    CHUNK_SIZE_LINES,
    CHUNK_OVERLAP,
    MAX_LINES_PER_FILE,
    EMBER_IGNORE_DIRS,
)


@dataclass
class FileChunk:
    file_path: str
    chunk_index: int
    start_line: int          # 1-based, inclusive
    end_line: int            # 1-based, inclusive
    total_lines: int
    content: str
    is_last: bool = False

    def header(self) -> str:
        return (
            f"### FILE: {self.file_path}  "
            f"| CHUNK {self.chunk_index + 1}  "
            f"| LINES {self.start_line}–{self.end_line} / {self.total_lines}\n"
        )

    def with_header(self) -> str:
        return self.header() + self.content


@dataclass
class ScanResult:
    file_path: str
    total_lines: int
    chunks: List[FileChunk] = field(default_factory=list)
    error: str | None = None

    def full_content(self) -> str:
        """Concatenate all chunks with positional headers."""
        parts = []
        seen_lines: set[int] = set()
        for chunk in self.chunks:
            # De-duplicate overlapping lines
            lines = chunk.content.splitlines(keepends=True)
            unique_lines = []
            for i, line in enumerate(lines):
                ln = chunk.start_line + i
                if ln not in seen_lines:
                    seen_lines.add(ln)
                    unique_lines.append(line)
            if unique_lines:
                header = (
                    f"\n### [{chunk.file_path}] LINES "
                    f"{chunk.start_line}–{chunk.end_line}\n"
                )
                parts.append(header + "".join(unique_lines))
        return "".join(parts)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


# ─────────────────────────────────────────────────────────────────────────────
# Core reader
# ─────────────────────────────────────────────────────────────────────────────

class LargeFileReader:
    """
    Reads arbitrarily large files in overlapping chunks.

    Parameters
    ----------
    chunk_size  : lines per chunk (default from settings)
    overlap     : lines repeated from the previous chunk (context continuity)
    max_lines   : absolute ceiling of lines read per file (≥20 000)
    encoding    : file encoding (falls back to latin-1 on decode errors)
    """

    def __init__(
        self,
        chunk_size: int = CHUNK_SIZE_LINES,
        overlap: int = CHUNK_OVERLAP,
        max_lines: int = MAX_LINES_PER_FILE,
        encoding: str = "utf-8",
    ):
        self.chunk_size = chunk_size
        self.overlap    = overlap
        self.max_lines  = max_lines
        self.encoding   = encoding

    # ── Public API ─────────────────────────────────────────────────────────────

    def read(self, file_path: str | Path) -> ScanResult:
        """Read a file and return a ScanResult with all chunks."""
        path = Path(file_path)
        if not path.exists():
            return ScanResult(file_path=str(file_path), total_lines=0, error="File not found")
        if not path.is_file():
            return ScanResult(file_path=str(file_path), total_lines=0, error="Not a file")

        try:
            lines = self._read_lines(path)
        except Exception as e:
            return ScanResult(file_path=str(file_path), total_lines=0, error=str(e))

        total = len(lines)
        capped_lines = lines[: self.max_lines]
        chunks = list(self._build_chunks(str(file_path), capped_lines, total))
        return ScanResult(file_path=str(file_path), total_lines=total, chunks=chunks)

    def iter_chunks(self, file_path: str | Path) -> Generator[FileChunk, None, None]:
        """Stream chunks one at a time (memory-efficient for very large files)."""
        result = self.read(file_path)
        yield from result.chunks

    def read_range(self, file_path: str | Path, start: int, end: int) -> str:
        """
        Read a specific line range (1-based, inclusive).
        Useful for targeted re-reads during migration.
        """
        path = Path(file_path)
        lines = self._read_lines(path)
        selected = lines[start - 1 : end]
        return "".join(selected)

    # ── Directory scanner ──────────────────────────────────────────────────────

    def scan_directory(
        self,
        directory: str | Path,
        extensions: list[str] | None = None,
        ignore_dirs: list[str] | None = None,
    ) -> list[ScanResult]:
        """
        Recursively scan a directory and return ScanResults for every file.

        Parameters
        ----------
        directory   : root path to scan
        extensions  : only include files with these extensions (e.g. ['.js', '.hbs'])
        ignore_dirs : directory names to skip (default: EMBER_IGNORE_DIRS)
        """
        root = Path(directory)
        skip = set(ignore_dirs or EMBER_IGNORE_DIRS)
        exts = set(extensions) if extensions else None
        results: list[ScanResult] = []

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories in-place so os.walk skips them
            dirnames[:] = [d for d in dirnames if d not in skip]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if exts and fpath.suffix not in exts:
                    continue
                results.append(self.read(fpath))

        return results

    def scan_files(self, file_paths: list[str | Path]) -> list[ScanResult]:
        """Read a specific list of files."""
        return [self.read(p) for p in file_paths]

    # ── Internals ──────────────────────────────────────────────────────────────

    def _read_lines(self, path: Path) -> list[str]:
        try:
            with path.open(encoding=self.encoding) as f:
                return f.readlines()
        except UnicodeDecodeError:
            with path.open(encoding="latin-1") as f:
                return f.readlines()

    def _build_chunks(
        self, file_path: str, lines: list[str], total_lines: int
    ) -> Generator[FileChunk, None, None]:
        step = self.chunk_size - self.overlap
        if step <= 0:
            step = self.chunk_size

        n = len(lines)
        idx = 0
        chunk_num = 0

        while idx < n:
            end_idx = min(idx + self.chunk_size, n)
            chunk_lines = lines[idx:end_idx]
            content = "".join(chunk_lines)

            start_line = idx + 1
            end_line   = end_idx
            is_last    = end_idx >= n

            yield FileChunk(
                file_path   = file_path,
                chunk_index = chunk_num,
                start_line  = start_line,
                end_line    = end_line,
                total_lines = total_lines,
                content     = content,
                is_last     = is_last,
            )

            if is_last:
                break
            idx       += step
            chunk_num += 1


# ─────────────────────────────────────────────────────────────────────────────
# Convenience singleton
# ─────────────────────────────────────────────────────────────────────────────

_default_reader = LargeFileReader()

def read_file(path: str | Path) -> ScanResult:
    return _default_reader.read(path)

def read_range(path: str | Path, start: int, end: int) -> str:
    return _default_reader.read_range(path, start, end)

def scan_directory(
    directory: str | Path,
    extensions: list[str] | None = None,
    ignore_dirs: list[str] | None = None,
) -> list[ScanResult]:
    return _default_reader.scan_directory(directory, extensions, ignore_dirs)
