"""Paperless document push (#931): store each upload in paperless with the
clean gemma4:12b vision text indexed for full-text search.

Paperless is a document store + Web-UI + full-text search only; its own
Tesseract OCR is skipped (the #929 PoC proved it garbles rotated German scans),
so paperless never derives the searchable text — Solaris does. For each upload
companion this adapter:

  1. runs the EXISTING gemma4:12b vision extractor to transcribe clean text
     (`think:false` + a ~300 KB downscaled first page — the PoC found the
     full-res pages return an EMPTY vision response);
  2. POSTs the original file to paperless (consumed OCR-skipped);
  3. PATCHes `/api/documents/{id}/` with `content=<that text>` so paperless's
     full-text search indexes the clean text.

Result: paperless owns the store/UI/search; Solaris's gemma4 stays the fact +
text source. Disabled (no-op) when `PAPERLESS_URL`/`PAPERLESS_TOKEN` are unset.
A `<!-- paperless -->` marker in the companion is the idempotency guard so a
re-run neither re-uploads nor re-transcribes.

Push timing is deliberately LAZY-via-cron, not eager-at-upload (#1042). The
push is gated on `EXTRACTED_MARKER`, so it can only run once the upload's text
has been extracted — and `napi_upload` already runs that extraction
fire-and-forget off the request path (pdftotext/OCR is too slow to block the
HTTP response). Since the push depends on that deferred step AND itself runs a
multi-second gemma4:12b vision transcription per document, it stays off the
upload path too: `run_ingest`/`push_uploads` drives it on the periodic ingest
cycle, marker-gated and idempotent, so a document lost to a restart is simply
picked up on the next pass. Eager-at-upload would buy a shorter store-to-search
lag at the cost of a slow, vision-heavy upload request — not worth it for a
document store whose search is not latency-critical.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from solaris_chat.logging import log

from ..ollama import OllamaChat, OllamaError
from .paperless_client import PaperlessClient, PaperlessTaskTimeout
from .upload_extract import (
    EXTRACTED_MARKER,
    companion_media,
    downscaled_vision_image,
)

PAPERLESS_MARKER = "<!-- paperless -->"

# The vision model + prompt that transcribe the downscaled page to clean text.
# think:false is load-bearing (the PoC found reasoning-on returns empty on this
# path); a plain "transcribe verbatim" prompt, no facts — paperless only needs
# the searchable text, the fact extraction stays in the nightly librarian turn.
_VISION_MODEL = "gemma4:12b"
_VISION_PROMPT = (
    "Transkribiere den GESAMTEN Text dieses Dokuments wortgetreu. "
    "Nur der Text, keine Erklärung, keine Zusammenfassung."
)


async def _vision_text(ollama: OllamaChat, image_b64: str) -> str:
    """Clean transcription of `image_b64` from the vision model, or "".

    think:false + the single downscaled image is the PoC-validated shape; any
    Ollama error degrades to "" (the doc is still stored, just without indexed
    text) rather than aborting the push."""
    messages = [{"role": "user", "content": _VISION_PROMPT, "images": [image_b64]}]
    try:
        result = None
        async for kind, payload in ollama.stream(_VISION_MODEL, messages, think=False):
            if kind == "done":
                result = payload
    except OllamaError as e:
        log.error("engine.ingest.paperless_vision_failed", error=str(e))
        return ""
    return result.content.strip() if result else ""


async def push_companion(
    companion_md: Path,
    notes_dir: str,
    ollama: OllamaChat,
    client: PaperlessClient,
) -> bool:
    """Push one upload companion to paperless. Never raises.

    Returns True when the document was stored (and marked), False when it was
    already pushed, isn't extracted yet, or the file is missing/dropped."""
    try:
        content = companion_md.read_text(encoding="utf-8")
    except OSError as e:
        log.error("engine.ingest.paperless_read_failed", error=str(e))
        return False
    # Only push once OCR ran (there is a document to store) and not already done.
    if EXTRACTED_MARKER not in content or PAPERLESS_MARKER in content:
        return False
    companion_rel = str(companion_md.relative_to(notes_dir))
    media = companion_media(notes_dir, companion_rel)
    if media is None:
        return False
    file_bytes, filename = media

    image_b64 = downscaled_vision_image(notes_dir, companion_rel)
    text = await _vision_text(ollama, image_b64) if image_b64 else ""

    try:
        doc_id = await client.post_document(file_bytes, filename)
    except PaperlessTaskTimeout as e:
        # The consume task didn't finish in the poll budget, so the doc id is
        # unknown *yet* and patch_content() can't run. Leave the companion UNMARKED
        # so the next ingest pass retries it — marking here would strand it with
        # paperless's garbled OCR content forever (#1053).
        log.info(
            "engine.ingest.paperless_push_deferred", doc=companion_rel, task=str(e)
        )
        return False
    if doc_id is not None and text:
        await client.patch_content(doc_id, text)
        # Surface paperless's own correspondent/doc-type/tag suggestions for human
        # review (#1050). On-demand only — nothing triggers it on add/update — and
        # it reads the same content[:4000] we just PATCHed. Best-effort: a failure
        # must not unmark or re-push the (already stored) doc.
        try:
            await client.trigger_ai_suggestions(doc_id)
        except Exception as e:  # noqa: BLE001 — suggestions are advisory, not the store.
            log.error("engine.ingest.paperless_ai_suggestions_failed", error=repr(e))

    try:
        companion_md.write_text(
            content.rstrip("\n") + f"\n\n{PAPERLESS_MARKER}\n", encoding="utf-8"
        )
    except OSError as e:  # marking is best-effort; the doc is already stored.
        log.error("engine.ingest.paperless_mark_failed", error=str(e))
    log.info("engine.ingest.paperless_pushed", doc=companion_rel, paperless_id=doc_id)
    return True


async def push_uploads(
    notes_dir: str, ollama: OllamaChat, client: PaperlessClient
) -> int:
    """Push every extracted-but-unpushed upload companion. Never raises.

    Globs the per-resident + shared upload folders (like `ingest_uploads`),
    pushes each, and returns the count stored this pass."""
    root = Path(notes_dir)
    companions = sorted(
        {*root.glob("users/*/uploads/*.md"), *root.glob("uploads/*.md")}
    )
    pushed = 0
    for companion in companions:
        try:
            if await push_companion(companion, notes_dir, ollama, client):
                pushed += 1
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the pass.
            # repr() + traceback, not str(): many failures here (aiohttp
            # ClientResponseError, a bare TimeoutError) stringify to "" or a lone
            # number, hiding the real cause — repr keeps the type + args and the
            # traceback pins the failing call.
            log.error(
                "engine.ingest.paperless_companion_failed",
                path=str(companion),
                error=repr(e),
                traceback=traceback.format_exc(),
            )
    log.info("engine.ingest.paperless", companions=len(companions), pushed=pushed)
    return pushed
