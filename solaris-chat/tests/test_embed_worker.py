"""embed_worker.drain() — queue → okf_vectors, crash-safe, dedup, resume.

Mirrors the 0016/0018 DDL inline (no alembic import — CI's solaris-chat env has
none; migration-apply verification lives in database/ or box-verify).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from solaris_chat.engine.knowledge import embed_worker, projection
from solaris_chat.engine.llama_server import LlamaServerError

# okf_vectors (0018) — the only table the worker touches.
_SCHEMA = """
CREATE TABLE okf_vectors (
  embedding_id TEXT PRIMARY KEY, concept_id TEXT NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vector BLOB NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
"""


class _FakeClient:
    """Stands in for LlamaEmbed: embed returns a per-text vector."""

    def __init__(self, *, embed_raises: bool = False):
        self.embed_raises = embed_raises
        self.embed_calls: list[list[str]] = []

    async def embed(self, model, inputs):
        self.embed_calls.append(list(inputs))
        if self.embed_raises:
            raise LlamaServerError("llama-server /v1/embeddings 503")
        return [[float(len(t)), 1.0, 2.0] for t in inputs]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "solaris.db"
    conn = projection.open_conn(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return str(path)


def _enqueue(db_path: str, lines: list[dict], *, suffix: str = ".jsonl") -> None:
    from pathlib import Path

    p = Path(db_path).with_name("okf_embedding_queue" + suffix)
    with p.open("a", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")


def _entry(eid: str, text: str) -> dict:
    return {
        "embedding_id": eid,
        "concept_id": f"ref-{eid}",
        "model": "nomic-embed-text",
        "text": text,
    }


async def test_drain_last_line_wins_and_deletes_queue(db_path, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(embed_worker, "LlamaEmbed", lambda url: client)
    # e1 appears twice — the second (longer text) must win.
    _enqueue(
        db_path,
        [_entry("e1", "aa"), _entry("e2", "bbb"), _entry("e1", "aaaaa")],
    )

    await embed_worker.drain(db_path, "http://x")

    from pathlib import Path

    assert not Path(db_path).with_name("okf_embedding_queue.jsonl").exists()
    assert not Path(db_path).with_name("okf_embedding_queue.jsonl.draining").exists()

    conn = projection.open_conn(db_path)
    rows = {r["embedding_id"]: r for r in conn.execute("SELECT * FROM okf_vectors")}
    conn.close()
    assert set(rows) == {"e1", "e2"}
    assert rows["e1"]["concept_id"] == "ref-e1"
    assert rows["e1"]["dim"] == 3
    # First float of the fake vector is len(text); last line ("aaaaa") wins.
    vec = np.frombuffer(rows["e1"]["vector"], dtype=np.float32)
    assert vec[0] == 5.0


async def test_drain_resumes_preexisting_draining_and_fresh_queue(db_path, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(embed_worker, "LlamaEmbed", lambda url: client)
    # A crashed run left a .draining file; a fresh queue also has work.
    _enqueue(db_path, [_entry("old", "x")], suffix=".jsonl.draining")
    _enqueue(db_path, [_entry("new", "y")], suffix=".jsonl")

    await embed_worker.drain(db_path, "http://x")

    conn = projection.open_conn(db_path)
    ids = {
        r["embedding_id"] for r in conn.execute("SELECT embedding_id FROM okf_vectors")
    }
    conn.close()
    assert ids == {"old", "new"}


async def test_drain_leaves_draining_when_the_embeddings_server_is_down(
    db_path, monkeypatch
):
    client = _FakeClient(embed_raises=True)
    monkeypatch.setattr(embed_worker, "LlamaEmbed", lambda url: client)
    _enqueue(db_path, [_entry("e1", "aa")])

    await embed_worker.drain(db_path, "http://x")

    from pathlib import Path

    # Nothing lost: the queue was moved to .draining and survives for a retry.
    draining = Path(db_path).with_name("okf_embedding_queue.jsonl.draining")
    assert draining.exists()
    conn = projection.open_conn(db_path)
    n = conn.execute("SELECT COUNT(*) AS n FROM okf_vectors").fetchone()["n"]
    conn.close()
    assert n == 0
