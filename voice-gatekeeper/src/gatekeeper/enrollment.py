"""HTTP enrolment endpoint for Phase 2 Speaker-ID (#937).

Exposes `POST /enrol` so an admin tool can submit N audio samples for
a resident `uid`, get them embedded, and store the averaged embedding
in `voice_embeddings`. The request body is JSON; audio is base64-
encoded little-endian int16 PCM (16 kHz mono).

This is intentionally not a Wyoming flow: a Wyoming-driven "say your
name 3 times" UX requires a satellite-side dialogue manager we don't
have yet. The HTTP form lets an operator drive enrolment from a
small CLI or the ServiceBay dashboard, with audio captured by any
microphone-aware client they have. The on-disk schema is the same
either way (one row per uid, averaged embedding).

Auth: shares `PUSH_TOKEN` with the push endpoint — same trust
boundary (pod-internal HTTP listener that's never exposed off-pod).
"""

from __future__ import annotations

import asyncio
import base64
import re

from aiohttp import web
from gatekeeper.logging import log

from .embeddings_store import (
    EMBEDDING_DIM,
    delete_embedding,
    insert_embedding,
    list_uids,
)
from .speaker import average_embeddings, get_extractor

_UID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _auth_ok(request: web.Request, token: str) -> bool:
    if not token:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {token}"


def add_routes(
    app: web.Application,
    *,
    db_path: str,
    push_token: str,
    sample_rate_hint: int = 16000,
    sample_width_hint: int = 2,
    channels_hint: int = 1,
    min_samples: int = 3,
    max_samples: int = 10,
) -> None:
    """Attach enrolment endpoints to an existing aiohttp app. Sharing
    the push app keeps the gatekeeper to a single sidecar HTTP port."""

    async def enrol(request: web.Request) -> web.Response:
        if not _auth_ok(request, push_token):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        uid = str(body.get("uid") or "").strip()
        if not _UID_RE.match(uid):
            return web.json_response({"ok": False, "reason": "invalid_uid"}, status=400)

        samples_b64 = body.get("samples")
        if not isinstance(samples_b64, list):
            return web.json_response(
                {"ok": False, "reason": "missing_samples"}, status=400
            )
        if not (min_samples <= len(samples_b64) <= max_samples):
            return web.json_response(
                {
                    "ok": False,
                    "reason": "sample_count_out_of_range",
                    "min": min_samples,
                    "max": max_samples,
                },
                status=400,
            )

        rate = int(body.get("sample_rate", sample_rate_hint))
        width = int(body.get("sample_width", sample_width_hint))
        channels = int(body.get("channels", channels_hint))

        extractor = get_extractor()
        if extractor is None:
            return web.json_response(
                {
                    "ok": False,
                    "reason": "speaker_id_disabled",
                    "hint": "set SOLARIS_SPEAKER_ID_ENABLED=1 and install [speaker-id] extras",
                },
                status=503,
            )

        try:
            decoded = [base64.b64decode(s) for s in samples_b64]
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "reason": "invalid_base64"}, status=400
            )

        loop = asyncio.get_running_loop()
        embeddings: list[bytes] = []
        for idx, pcm in enumerate(decoded):
            try:
                emb = await loop.run_in_executor(
                    None, extractor.extract, pcm, rate, width, channels
                )
            except TypeError:
                # Older Python: pass kwargs via lambda since run_in_executor
                # doesn't forward keyword args.
                emb = await loop.run_in_executor(
                    None,
                    lambda p=pcm: extractor.extract(
                        p, rate=rate, width=width, channels=channels
                    ),
                )
            if emb is None:
                log.warn("gatekeeper.enrol.sample_skipped", uid=uid, sample=idx)
                continue
            if len(emb) != EMBEDDING_DIM * 4:
                log.warn(
                    "gatekeeper.enrol.bad_dim", uid=uid, sample=idx, bytes=len(emb)
                )
                continue
            embeddings.append(emb)

        if len(embeddings) < min_samples:
            return web.json_response(
                {
                    "ok": False,
                    "reason": "not_enough_usable_samples",
                    "got": len(embeddings),
                    "needed": min_samples,
                },
                status=422,
            )

        try:
            averaged = average_embeddings(embeddings)
        except ValueError as exc:
            return web.json_response({"ok": False, "reason": str(exc)}, status=422)

        await asyncio.to_thread(
            insert_embedding,
            db_path,
            uid,
            averaged,
            sample_count=len(embeddings),
            enrolled_via="http",
        )
        log.info("gatekeeper.enrol.ok", uid=uid, samples_used=len(embeddings))
        return web.json_response(
            {"ok": True, "uid": uid, "samples_used": len(embeddings)}
        )

    async def list_enrolments(request: web.Request) -> web.Response:
        if not _auth_ok(request, push_token):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        uids = await asyncio.to_thread(list_uids, db_path)
        return web.json_response({"uids": uids})

    async def delete_enrolment(request: web.Request) -> web.Response:
        if not _auth_ok(request, push_token):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        uid = request.match_info.get("uid", "")
        if not _UID_RE.match(uid):
            return web.json_response({"ok": False, "reason": "invalid_uid"}, status=400)
        removed = await asyncio.to_thread(delete_embedding, db_path, uid)
        return web.json_response({"ok": True, "removed": removed})

    app.router.add_post("/enrol", enrol)
    app.router.add_get("/enrolments", list_enrolments)
    app.router.add_delete("/enrolments/{uid}", delete_enrolment)
