"""Whole-concept embedding enqueue (docs/okf-write-contract.md §5).

Policy: one vector per concept (title + description + body), model
`nomic-embed-text`, stored keyed by `concepts.embedding_id`, (re-)embedded only
when `content_hash` changed.

The writer does not embed inline — it enqueues the work through this small
interface. The default `PendingEmbeddingQueue` durably records the pending
`(embedding_id, concept_id, text)` triples to a JSONL sidecar next to
`solaris.db`; `embed_worker.drain()` consumes it, calls `nomic-embed-text` on
the embeddings llama-server and upserts one vector per `embedding_id` into
`okf_vectors`. `enqueue` returns the `embedding_id` to store on the `concepts`
row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class EmbeddingQueue(Protocol):
    def enqueue(self, *, concept_id: str, embedding_id: str, text: str) -> str:
        """Enqueue a whole-concept (re-)embedding; return the `embedding_id`."""
        ...


class NullEmbeddingQueue:
    """Drops the work — for tests/adapters that don't exercise embedding."""

    def enqueue(self, *, concept_id: str, embedding_id: str, text: str) -> str:
        return embedding_id


class PendingEmbeddingQueue:
    """Durably records pending embeddings to an append-only JSONL sidecar.

    Each enqueue appends ONE line (O(1)) — re-reading/rewriting the whole file
    per write was O(n^2) and pegged a core for hours on the full catalog (#597).
    The same `embedding_id` may therefore appear more than once (a re-embed
    appends a fresh line); the (not-yet-built) drain worker dedups by keeping
    the LAST line per `embedding_id`.
    """

    def __init__(self, db_path: str):
        self._path = Path(db_path).with_name("okf_embedding_queue.jsonl")
        # The pre-#597 sidecar was a single whole-file JSON dict under the .json
        # name; nothing drains it, so rotate it aside once rather than convert.
        legacy = Path(db_path).with_name("okf_embedding_queue.json")
        if legacy.exists():
            legacy.rename(legacy.with_suffix(".json.legacy"))

    def enqueue(self, *, concept_id: str, embedding_id: str, text: str) -> str:
        entry = {
            "embedding_id": embedding_id,
            "concept_id": concept_id,
            "model": "nomic-embed-text",
            "text": text,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # embed_worker.drain() consumes this file: dedups by embedding_id (last
        # line wins), calls nomic-embed-text, upserts the vector into okf_vectors.
        return embedding_id
