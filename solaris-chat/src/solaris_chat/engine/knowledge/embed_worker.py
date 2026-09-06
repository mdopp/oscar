"""Drain the OKF embedding queue into the `okf_vectors` store (#650).

`PendingEmbeddingQueue` (embedding.py) appends one JSONL line per (re-)embed to
`okf_embedding_queue.jsonl` next to `solaris.db`; the same `embedding_id` may
repeat (a re-embed appends), **last line wins**. This worker consumes those
lines, calls `nomic-embed-text`, and upserts one float32 vector per
`embedding_id` into `okf_vectors`.

`drain()` is a plain async function invoked from the tail of `run_ingest()` (no
new thread/task/knob): that single call site covers "at boot" and "after every
ingest run", and the nightly pipeline (#652) re-runs `run_ingest()` for
"periodically". It must never run on the voice hot path — a batch of 64 keeps
the embeddings server busy for seconds at a time.

Note: `okf_vectors.concept_id` carries the writer's `ref_id` (the entity/event
id), NOT `concepts.id`. Retrieval joins through
`concepts.embedding_id = okf_vectors.embedding_id`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from solaris_chat.logging import log

from ..llama_embed import LlamaEmbed
from ..llama_server import LlamaServerError
from . import projection

_MODEL = "nomic-embed-text"
_BATCH = 64


async def drain(db_path: str, embed_url: str) -> None:
    """Consume the embedding queue into `okf_vectors`. Never raises.

    Crash-safe: the live queue is `os.rename`d to a `.draining` sidecar (atomic;
    writers append to a fresh queue thereafter) and processed from there, so a
    crash mid-drain resumes from the `.draining` file on the next run rather than
    losing lines. If the embeddings server can't be reached, the `.draining`
    file is left in place and the next drain retries it.
    """
    try:
        queue_path = Path(db_path).with_name("okf_embedding_queue.jsonl")
        draining_path = queue_path.with_name(queue_path.name + ".draining")

        # A crashed drain left older lines in `.draining`; read them into memory
        # FIRST (renaming the live queue over the path would clobber the file),
        # then move the live queue aside so writers append to a fresh file. Order
        # matters for last-line-wins: the resumed lines are older than the queue.
        blocks: list[str] = []
        if draining_path.exists():
            blocks.append(draining_path.read_text(encoding="utf-8"))
        if queue_path.exists():
            os.rename(queue_path, draining_path)
            blocks.append(draining_path.read_text(encoding="utf-8"))

        if not blocks:
            return

        entries: dict[str, dict] = {}
        skipped = 0
        for block in blocks:
            for raw in block.splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries[entry["embedding_id"]] = entry
                except (json.JSONDecodeError, KeyError, TypeError):
                    skipped += 1

        if not entries:
            draining_path.unlink(missing_ok=True)
            return

        if not embed_url:
            log.warning("engine.embed.no_server", pending=len(entries))
            return  # leave .draining in place; next drain resumes it.
        client = LlamaEmbed(embed_url)

        items = list(entries.values())
        conn = projection.open_conn(db_path)
        try:
            drained = 0
            for start in range(0, len(items), _BATCH):
                batch = items[start : start + _BATCH]
                try:
                    vectors = await client.embed(_MODEL, [e["text"] for e in batch])
                except (LlamaServerError, OSError) as e:
                    # Leave `.draining` in place: the lines are still owed and
                    # the next run resumes them. A coding lease stops the
                    # embeddings server outright, so this is a normal state.
                    log.warning(
                        "engine.embed.server_unreachable",
                        pending=len(items) - drained,
                        error=str(e),
                    )
                    return
                for entry, vec in zip(batch, vectors, strict=True):
                    blob = np.asarray(vec, dtype=np.float32).tobytes()
                    conn.execute(
                        """
                        INSERT INTO okf_vectors
                          (embedding_id, concept_id, model, dim, vector)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(embedding_id) DO UPDATE SET
                          concept_id = excluded.concept_id,
                          model = excluded.model,
                          dim = excluded.dim,
                          vector = excluded.vector,
                          updated = datetime('now')
                        """,
                        (
                            entry["embedding_id"],
                            entry["concept_id"],
                            entry.get("model") or _MODEL,
                            len(vec),
                            blob,
                        ),
                    )
                conn.commit()
                drained += len(batch)
        finally:
            conn.close()

        draining_path.unlink(missing_ok=True)
        log.info("engine.embed.drained", drained=drained, skipped=skipped)
    except Exception as e:  # noqa: BLE001 — the drain must never crash the ingest.
        log.error("engine.embed.drain_failed", error=str(e))
