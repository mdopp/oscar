"""Paperless document push (#931).

The paperless REST client and the Ollama vision call are both faked (no live
instance, no model): these cover the PoC-validated handoff — gemma4:12b vision
(think:false + a downscaled image) → POST the file OCR-skipped → PATCH the clean
text into paperless full-text search — plus the idempotency marker, the
disabled-when-unconfigured no-op, and the graceful degrade when the file is
missing or paperless dedups the upload.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from solaris_chat.config import Settings
from solaris_chat.engine.ingest import paperless
from solaris_chat.engine.ingest.paperless import (
    PAPERLESS_MARKER,
    push_companion,
    push_uploads,
)
from solaris_chat.engine.ingest import paperless_client
from solaris_chat.engine.ingest.paperless_client import RestPaperlessClient
from solaris_chat.engine.ingest.runner import _run_paperless
from solaris_chat.engine.ingest.upload_extract import EXTRACTED_MARKER


class FakePaperlessClient:
    """Records the POST/PATCH the push makes so a test can assert the handoff."""

    def __init__(self, *, doc_id: int | None = 42):
        self._doc_id = doc_id
        self.posted: list[tuple[bytes, str]] = []
        self.patched: list[tuple[int, str]] = []
        self.suggested: list[int] = []

    async def post_document(self, file_bytes: bytes, filename: str) -> int | None:
        self.posted.append((file_bytes, filename))
        return self._doc_id

    async def patch_content(self, document_id: int, content: str) -> None:
        self.patched.append((document_id, content))

    async def trigger_ai_suggestions(self, document_id: int) -> None:
        self.suggested.append(document_id)


class FakeOllama:
    """A vision model that returns canned text and records how it was called."""

    def __init__(self, text: str = "Sauberer Vision-Text"):
        self._text = text
        self.calls: list[dict] = []

    async def stream(self, model, messages, think=True, **kwargs):
        self.calls.append({"model": model, "messages": messages, "think": think})

        class _Result:
            content = self._text

        yield "done", _Result()


def _companion(vault: Path, rel: str, *, extracted: bool = True) -> Path:
    """Write an upload companion + its sibling media under `vault`."""
    md = vault / rel
    md.parent.mkdir(parents=True, exist_ok=True)
    body = "# Scan\n"
    if extracted:
        body += f"\n{EXTRACTED_MARKER}\n## Inhalt (extrahiert)\n\nOCR-Text\n"
    md.write_text(body, encoding="utf-8")
    (md.parent / f"{md.stem}.pdf").write_bytes(b"%PDF-1.4 fake")
    return md


def _push(md: Path, vault: Path, ollama, client, *, image="IMGB64"):
    """Run push_companion with the downscale helper stubbed to a fixed image."""
    orig = paperless.downscaled_vision_image
    paperless.downscaled_vision_image = lambda *_: image
    try:
        return asyncio.run(push_companion(md, str(vault), ollama, client))
    finally:
        paperless.downscaled_vision_image = orig


def test_handoff_posts_file_then_patches_vision_text(tmp_path):
    md = _companion(tmp_path, "users/mdopp/uploads/scan.md")
    client = FakePaperlessClient(doc_id=7)
    ollama = FakeOllama("Klarer deutscher Text")

    assert _push(md, tmp_path, ollama, client) is True

    # POST the ORIGINAL file (paperless stores it; OCR-skip is the deployment's
    # PAPERLESS_OCR_MODE — no per-request OCR flag is sent).
    assert client.posted == [(b"%PDF-1.4 fake", "scan.pdf")]
    # PATCH the created doc's content with the vision text so search re-indexes it.
    assert client.patched == [(7, "Klarer deutscher Text")]
    # …then fire paperless's AI classifier so suggestions surface for review (#1050).
    assert client.suggested == [7]


def test_ai_suggestions_failure_degrades_but_still_marks(tmp_path, monkeypatch):
    # #1050: the suggestion trigger is advisory — if it raises, the doc is already
    # stored + PATCHed, so the companion must still be marked (not re-pushed).
    md = _companion(tmp_path, "users/mdopp/uploads/scan.md")

    class SuggestBoom(FakePaperlessClient):
        async def trigger_ai_suggestions(self, document_id):
            raise RuntimeError("classifier down")

    records: list[dict] = []
    monkeypatch.setattr(
        paperless.log, "error", lambda msg, **kw: records.append({"msg": msg, **kw})
    )
    client = SuggestBoom(doc_id=9)
    assert _push(md, tmp_path, FakeOllama(), client) is True
    assert client.patched == [(9, "Sauberer Vision-Text")]
    assert PAPERLESS_MARKER in md.read_text(encoding="utf-8")
    assert any(
        r["msg"] == "engine.ingest.paperless_ai_suggestions_failed" for r in records
    )


def test_vision_call_uses_think_false_and_the_downscaled_image(tmp_path):
    md = _companion(tmp_path, "users/mdopp/uploads/scan.md")
    ollama = FakeOllama()
    _push(md, tmp_path, ollama, FakePaperlessClient(), image="DOWNSCALED")

    assert len(ollama.calls) == 1
    call = ollama.calls[0]
    assert call["model"] == "gemma4:12b"
    assert call["think"] is False
    # The single downscaled page image is what the model sees (the PoC found the
    # full-res pages return empty).
    assert call["messages"][0]["images"] == ["DOWNSCALED"]


def test_marker_makes_it_idempotent(tmp_path):
    md = _companion(tmp_path, "users/mdopp/uploads/scan.md")
    client = FakePaperlessClient()
    assert _push(md, tmp_path, FakeOllama(), client) is True
    assert PAPERLESS_MARKER in md.read_text(encoding="utf-8")
    # Second pass: already marked → no re-upload, no re-transcribe.
    client2 = FakePaperlessClient()
    ollama2 = FakeOllama()
    assert _push(md, tmp_path, ollama2, client2) is False
    assert client2.posted == [] and ollama2.calls == []


def test_unextracted_companion_is_not_pushed(tmp_path):
    md = _companion(tmp_path, "users/mdopp/uploads/scan.md", extracted=False)
    client = FakePaperlessClient()
    assert _push(md, tmp_path, FakeOllama(), client) is False
    assert client.posted == []


def test_dedup_drop_skips_patch_but_still_marks(tmp_path):
    # paperless returns no doc id (it deduped the re-upload) → nothing to PATCH,
    # but the companion is still marked so the pass doesn't retry forever.
    md = _companion(tmp_path, "users/mdopp/uploads/scan.md")
    client = FakePaperlessClient(doc_id=None)
    assert _push(md, tmp_path, FakeOllama(), client) is True
    assert client.patched == []
    assert PAPERLESS_MARKER in md.read_text(encoding="utf-8")


def test_push_uploads_globs_resident_and_shared(tmp_path):
    _companion(tmp_path, "users/mdopp/uploads/a.md")
    _companion(tmp_path, "uploads/b.md")
    client = FakePaperlessClient()
    orig = paperless.downscaled_vision_image
    paperless.downscaled_vision_image = lambda *_: "IMG"
    try:
        pushed = asyncio.run(push_uploads(str(tmp_path), FakeOllama(), client))
    finally:
        paperless.downscaled_vision_image = orig
    assert pushed == 2
    assert len(client.posted) == 2


def test_companion_failure_logs_exception_type_and_traceback(tmp_path, monkeypatch):
    # #1046: a raising client failed silently — str(exc) on a TimeoutError/aiohttp
    # error stringified to "" or "0", so the real cause never surfaced. The pass
    # must keep going AND log the exception repr + a traceback.
    _companion(tmp_path, "users/mdopp/uploads/a.md")

    class BoomClient:
        async def post_document(self, file_bytes, filename):
            raise TimeoutError()  # str() == "" — the failure the issue reproduced.

        async def patch_content(self, document_id, content):  # pragma: no cover
            pass

    records: list[dict] = []
    monkeypatch.setattr(
        paperless.log, "error", lambda msg, **kw: records.append({"msg": msg, **kw})
    )
    orig = paperless.downscaled_vision_image
    paperless.downscaled_vision_image = lambda *_: "IMG"
    try:
        pushed = asyncio.run(push_uploads(str(tmp_path), FakeOllama(), BoomClient()))
    finally:
        paperless.downscaled_vision_image = orig

    assert pushed == 0
    failed = [
        r for r in records if r["msg"] == "engine.ingest.paperless_companion_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["error"] == "TimeoutError()"  # repr keeps the type
    assert "TimeoutError" in failed[0]["traceback"]


class _FakeGet:
    """Stand-in for `session.get(...)` async-context returning canned JSON."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        payload = self._payload

        class _Resp:
            status = 200

            async def json(self):
                return payload

        return _Resp()

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *_a, **_k):
        return _FakeGet(self._payload)


def _resolve(payload, monkeypatch):
    client = RestPaperlessClient("http://127.0.0.1:8000", "tok")

    async def _no_sleep(_):  # the poll backoff would otherwise stall the test.
        return None

    monkeypatch.setattr(paperless_client.asyncio, "sleep", _no_sleep)
    return asyncio.run(client._resolve_document_id(_FakeSession(payload), "task-uuid"))


def test_resolve_document_id_reads_drf_paginated_dict(monkeypatch):
    # #1048: paperless-ngx :beta (API v10+) wraps GET /api/tasks/?task_id=… in DRF
    # pagination — a dict, not a list. Indexing tasks[0] on the dict raised
    # KeyError(0) on every push. The client must read `results`.
    payload = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [{"status": "SUCCESS", "related_document": 314}],
    }
    assert _resolve(payload, monkeypatch) == 314


def test_resolve_document_id_reads_bare_list_pre_v10(monkeypatch):
    # pre-v10 paperless returned a bare list; keep that path working.
    payload = [{"status": "SUCCESS", "related_document": 7}]
    assert _resolve(payload, monkeypatch) == 7


def test_run_paperless_no_op_when_unconfigured(monkeypatch):
    for key in ("PAPERLESS_URL", "PAPERLESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.paperless_url == "" and settings.paperless_token == ""

    called = False

    async def _boom(*_a, **_k):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(paperless, "push_uploads", _boom)
    # Unconfigured → returns immediately, never touching the push path.
    asyncio.run(_run_paperless(settings))
    assert called is False


def test_config_reads_paperless_env(monkeypatch):
    monkeypatch.setenv("PAPERLESS_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "tok123")
    settings = Settings.from_env()
    assert settings.paperless_url == "http://127.0.0.1:8000"
    assert settings.paperless_token == "tok123"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
