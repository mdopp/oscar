"""Web Push notifier — fan a timer/reminder out to a resident's phones (#713).

The timer scheduler's speaker announce stays primary; this is a best-effort
second channel so a reminder reaches the resident away from the Voice PE
speaker, through the installed PWA's service worker (standard Web Push / VAPID,
no Google/FCM). Constructed once at boot and injected into `TimerScheduler`.

No-op when VAPID is unset (`enabled` is False): the keys are an operator
prerequisite, not in the repo, so nothing breaks on the box before they are
configured — `push()` returns immediately. Errors are logged and swallowed; a
404/410 prunes the dead endpoint. The notifier must never break the timer loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from solaris_chat import push_store
from solaris_chat.engine import store
from solaris_chat.logging import log

# The typed event kinds the bus carries (#714). `card_state` is a live HA card
# update, `reminder` a fired timer, `chat` a backgrounded turn (Phase 1c),
# `servicebay` a republished ServiceBay approval event (BFF, #811).
EVENT_KINDS = frozenset({"reminder", "card_state", "chat", "servicebay"})


class EventBus:
    """In-process asyncio pub/sub for live-status propagation, keyed by uid.

    One process, one box: a plain in-memory fan-out (the #341 no-broker
    decision the SessionBus already follows). A subscriber registers a queue
    for its uid and drains typed events (`reminder · card_state · chat`);
    `publish(uid, kind, data)` fans the event out to every queue of that uid.
    Per-resident privacy: an event is delivered only to the uid it targets, so
    one resident never observes another's card state.

    An open SSE client subscribes; the HA-WS watcher and the timer scheduler
    publish; the `Notifier` (web push) is one more consumer, driven selectively
    for noteworthy events when no SSE client is listening for that uid.
    """

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    def publish(self, uid: str, kind: str, data: dict[str, Any]) -> None:
        event = {"kind": kind, "data": data}
        for q in self._subs.get(uid, set()):
            q.put_nowait(event)

    def has_subscriber(self, uid: str) -> bool:
        """True when a client currently holds an open subscription for `uid` —
        the selective-push gate: an SSE client already got the event live."""
        return bool(self._subs.get(uid))

    async def subscribe(self, uid: str):
        """Yield `{kind, data}` events for `uid` until the client drops."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(uid, set()).add(q)
        try:
            while True:
                yield await q.get()
        finally:
            subs = self._subs.get(uid)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subs.pop(uid, None)


def _vapid_private_scalar(private_key: str) -> str:
    """Normalise a VAPID private key to the raw base64url scalar pywebpush wants.

    `pywebpush.webpush(vapid_private_key=…)` accepts a file path or a raw 32-byte
    base64url scalar, NOT inline PEM (#804) — an operator-configured PEM was
    passed through raw and every send died with an ASN.1 parse error. When the key
    is PEM (starts with `-----`), load it via the shared config loader and emit the
    private scalar as base64url (no padding); a raw scalar is already accepted, so
    pass it through. A key that won't load is left as-is (push is already disabled
    upstream when the derived public key is empty)."""
    import base64

    from solaris_chat.config import _load_vapid_private_key

    if not private_key.strip().startswith("-----"):
        return private_key
    try:
        scalar = _load_vapid_private_key(private_key).private_numbers().private_value
    except Exception:  # noqa: BLE001 — a bad key already disables push, never crash
        return private_key
    return base64.urlsafe_b64encode(scalar.to_bytes(32, "big")).rstrip(b"=").decode()


def _reply_preview(reply: str, limit: int = 140) -> str:
    """One-line preview of an assistant reply for a notification body."""
    line = " ".join(reply.split())
    return line[: limit - 1] + "…" if len(line) > limit else line


async def emit_chat(
    bus: EventBus,
    notifier: Notifier | None,
    uid: str,
    session_id: str,
    reply: str,
    *,
    push: bool = True,
    card: dict[str, Any] | None = None,
) -> None:
    """Fan a completed chat turn out (Phase 1c, #715).

    Always publishes a `chat` event to the owner's SSE stream. When `push` is
    set and no SSE client is watching that uid (the app is backgrounded), also
    sends a Web Push with the session deep-link so the reply surfaces on the
    phone. `push=False` is the foreground streaming client — it already sees the
    reply live, so it must never self-notify; the SSE fan-out still runs so
    other open tabs update. `card` is a server-injected card (#785) carried on
    the SSE event for an open client to render.
    """
    preview = _reply_preview(reply)
    url = f"/#/c/{session_id}"
    data = {"kind": "chat", "session_id": session_id, "url": url}
    event = {"session_id": session_id, "preview": preview, "url": url}
    if card is not None:
        event["card"] = card
    bus.publish(uid, "chat", event)
    if push and notifier is not None and not bus.has_subscriber(uid):
        await notifier.push(uid, "Solaris", preview, data)


async def inject(
    db_path: str,
    bus: EventBus,
    notifier: Notifier | None,
    session_id: str,
    uid: str,
    text: str,
    *,
    card: dict[str, Any] | None = None,
) -> None:
    """Server-initiated turn into a resident's chat (Wartung P1a, #785).

    Nothing else can post into a chat except a live user turn; the Wartung chat
    (update/approval cards, #784) needs the server to speak first. Persists an
    assistant turn so it's present when the chat opens, then reuses the #715
    chat-propagation path: an open SSE client sees it live, a backgrounded phone
    gets a Web Push with the session deep-link. An optional `card` rides on the
    emitted event so an open client can render it; it is not fed back to the
    model, so it stays off the persisted history.
    """
    store.append_message(
        db_path,
        session_id,
        "assistant",
        text,
        conversation_id=store.resolve_conversation_id(db_path, session_id),
    )
    await emit_chat(bus, notifier, uid, session_id, text, card=card)


class Notifier:
    def __init__(
        self,
        db_path: str,
        vapid_public_key: str = "",
        vapid_private_key: str = "",
        vapid_subject: str = "",
    ):
        self._db_path = db_path
        self._public_key = vapid_public_key
        self._private_key = _vapid_private_scalar(vapid_private_key)
        self._subject = vapid_subject or "mailto:admin@solaris.local"

    @property
    def enabled(self) -> bool:
        return bool(self._public_key and self._private_key)

    async def push(
        self, uid: str, title: str, body: str, data: dict[str, Any] | None = None
    ) -> None:
        """Send a notification to every device the resident registered.

        No-op without VAPID. Prunes an endpoint the browser reports gone
        (404/410); every other failure is logged and swallowed."""
        if not self.enabled:
            return
        subs = push_store.list_for_uid(self._db_path, uid)
        if not subs:
            return
        payload = json.dumps({"title": title, "body": body, "data": data or {}})
        for sub in subs:
            await asyncio.to_thread(self._send_one, sub, payload)

    def _send_one(self, sub: dict[str, Any], payload: str) -> None:
        from pywebpush import WebPushException, webpush

        endpoint = sub["endpoint"]
        try:
            webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
            )
            push_store.mark_ok(self._db_path, endpoint)
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                push_store.remove_by_endpoint(self._db_path, endpoint)
                log.info("engine.push.pruned", endpoint=endpoint, status=status)
            else:
                log.error("engine.push.failed", endpoint=endpoint, error=str(e))
        except Exception as e:  # noqa: BLE001 — push must never break the timer loop
            log.error("engine.push.error", endpoint=endpoint, error=str(e))
