"""
Embedding-based vector store for context retrieval during migration.

Uses APIM text-embedding-3-large to encode code chunks, then performs
cosine-similarity search to retrieve the most relevant context for any
agent query.  Persisted to disk as a JSON file (no external DB required).

Why this matters
────────────────
When migrating a 20k-line module the LLM context window is finite.
Instead of cramming everything, we retrieve only the top-K relevant chunks
for each migration step, keeping prompts focused and costs low.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from ember_to_angular.tools.llm_client import get_embeddings
from ember_to_angular.config.settings import VECTOR_STORE_PATH, MODEL_EMBEDDING

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VectorEntry:
    id: str
    text: str
    metadata: dict
    embedding: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VectorEntry":
        return cls(**d)


@dataclass
class SearchResult:
    entry: VectorEntry
    score: float          # cosine similarity 0–1

    def snippet(self, max_chars: int = 500) -> str:
        return self.entry.text[:max_chars]


# ─────────────────────────────────────────────────────────────────────────────
# Vector store
# ─────────────────────────────────────────────────────────────────────────────

class CodeVectorStore:
    """
    In-process vector store backed by a JSON file.

    Not designed for millions of entries but perfectly sufficient for a single
    Ember project migration (typically a few thousand code chunks).
    """

    def __init__(
        self,
        store_path: str = VECTOR_STORE_PATH,
        embedding_model: str = MODEL_EMBEDDING,
        batch_size: int = 16,
    ):
        self._path   = Path(store_path) / "store.json"
        self._model  = embedding_model
        self._batch  = batch_size
        self._entries: List[VectorEntry] = []
        self._load()

    # ── Indexing ────────────────────────────────────────────────────────────────

    def add(self, id: str, text: str, metadata: dict | None = None) -> bool:
        """Add a single entry (calls embedding API if not already present)."""
        if self._get(id):
            return False   # already indexed
        entry = VectorEntry(id=id, text=text, metadata=metadata or {})
        embedding = self._embed([text])
        if not embedding:
            logger.warning("Could not embed entry %s; storing without embedding.", id)
        else:
            entry.embedding = embedding[0]
        self._entries.append(entry)
        return True

    def add_batch(self, items: list[tuple[str, str, dict]]) -> int:
        """
        Add multiple items at once.

        items: list of (id, text, metadata)
        Returns number of newly added entries.
        """
        new_items = [(id_, text, meta) for id_, text, meta in items if not self._get(id_)]
        if not new_items:
            return 0

        texts = [text for _, text, _ in new_items]
        embeddings = self._embed(texts)

        added = 0
        for i, (id_, text, meta) in enumerate(new_items):
            emb = embeddings[i] if (embeddings and i < len(embeddings)) else []
            self._entries.append(VectorEntry(id=id_, text=text, metadata=meta, embedding=emb))
            added += 1

        self._save()
        return added

    def index_scan_results(self, scan_results, prefix: str = "") -> int:
        """
        Convenience: index all chunks from a list of ScanResult objects.
        """
        items: list[tuple[str, str, dict]] = []
        for sr in scan_results:
            for chunk in sr.chunks:
                entry_id = f"{prefix}{chunk.file_path}::chunk{chunk.chunk_index}"
                meta = {
                    "file_path":   chunk.file_path,
                    "chunk_index": chunk.chunk_index,
                    "start_line":  chunk.start_line,
                    "end_line":    chunk.end_line,
                    "total_lines": chunk.total_lines,
                }
                items.append((entry_id, chunk.content, meta))
        return self.add_batch(items)

    # ── Search ──────────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5, filter_meta: dict | None = None) -> List[SearchResult]:
        """
        Return the top_k most relevant entries for `query`.

        filter_meta: if provided, only entries whose metadata contains all
                     key-value pairs in filter_meta are considered.
        """
        if not self._entries:
            return []

        q_emb = self._embed([query])
        if not q_emb:
            logger.warning("Could not embed query; returning random top entries.")
            return [SearchResult(e, 0.0) for e in self._entries[:top_k]]

        q_vec = q_emb[0]
        scored: List[SearchResult] = []

        for entry in self._entries:
            if filter_meta:
                if not all(entry.metadata.get(k) == v for k, v in filter_meta.items()):
                    continue
            if not entry.embedding:
                continue
            score = self._cosine(q_vec, entry.embedding)
            scored.append(SearchResult(entry=entry, score=score))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def search_text(self, query: str, top_k: int = 5) -> str:
        """
        Return the top results formatted as a single context block for LLM injection.
        """
        results = self.search(query, top_k=top_k)
        if not results:
            return "(no relevant context found)"

        parts = []
        for r in results:
            meta = r.entry.metadata
            header = (
                f"[{meta.get('file_path', '?')}  "
                f"lines {meta.get('start_line', '?')}–{meta.get('end_line', '?')}  "
                f"score={r.score:.3f}]"
            )
            parts.append(header + "\n" + r.entry.text)
        return "\n\n---\n\n".join(parts)

    # ── Persistence ─────────────────────────────────────────────────────────────

    def save(self):
        self._save()

    def clear(self):
        self._entries = []
        self._save()

    @property
    def size(self) -> int:
        return len(self._entries)

    # ── Internals ────────────────────────────────────────────────────────────────

    def _get(self, id_: str) -> Optional[VectorEntry]:
        for e in self._entries:
            if e.id == id_:
                return e
        return None

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed in batches with retry."""
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            batch = texts[i : i + self._batch]
            for attempt in range(3):
                result = get_embeddings(batch, self._model)
                if result:
                    all_embeddings.extend(result)
                    break
                wait = 2 ** attempt
                logger.warning("Embedding failed (attempt %d); retrying in %ss…", attempt + 1, wait)
                time.sleep(wait)
            else:
                logger.error("Embedding permanently failed for batch starting at index %d", i)
                return None
        return all_embeddings or None

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot  = sum(x * y for x, y in zip(a, b))
        na   = math.sqrt(sum(x * x for x in a))
        nb   = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _load(self):
        if self._path.exists():
            try:
                with self._path.open(encoding="utf-8") as f:
                    data = json.load(f)
                self._entries = [VectorEntry.from_dict(d) for d in data]
                logger.info("Loaded %d entries from vector store.", len(self._entries))
            except Exception as e:
                logger.warning("Could not load vector store: %s", e)
                self._entries = []
        else:
            self._entries = []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            json.dump([e.to_dict() for e in self._entries], f)
