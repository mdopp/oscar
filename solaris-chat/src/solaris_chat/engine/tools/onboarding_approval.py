"""Resident-onboarding approval + provisioning — the admin side of #355.

The guest onboarding flow (#376) enrols an unknown speaker's voice and files a
`pending_residents` row. This module is the admin-gated step that surfaces that
request to the human and finishes the provisioning once they approve:

  file_resident_approval(uid)  → files the pending request onto ServiceBay's
      central access-request list (the one place the admin already approves
      Authelia/SB access in, #343). Passing the uid as the LLDAP `username`
      lets the admin one-click Approve to auto-provision the SSO account — so
      *SB* owns the Authelia account; Solaris only owns the resident record and
      the voice-profile binding. The returned request id is stored on the row.

  check_resident_approval(uid) → polls that request id via
      get_access_request_status, which returns one of pending / approved /
      denied / not-found (servicebay#1824). While "pending" nothing changes.
      On "approved" Solaris provisions its side: it marks the pending row
      approved and confirms the enrolled voice profile (voice_embeddings,
      written by the gatekeeper under this same uid during onboarding) is
      present and bound to the uid. On "denied" Solaris provisions nothing and
      drops the captured biometrics: it deletes the candidate's voice profile
      via the gatekeeper's DELETE /enrolments/{uid} (the same cross-container
      seam enrolment uses — the gatekeeper owns voice_embeddings writes) and
      marks the local row denied. "not-found" means SB no longer knows the
      request, so the local pending row is closed the same way (provision
      nothing). The admin is the gate — Solaris never approves itself.

Biometric care, as elsewhere: no embedding bytes ever cross this module — only
the uid, display name and a present/absent verdict.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import aiohttp

from solaris_chat import pending_residents_store
from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.mcp_tools import call_sb_tool

# Same uid shape the gatekeeper's /enrolments/{uid} enforces.
_UID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _delete_voice_profile(
    gatekeeper_url: str, gatekeeper_token: str, uid: str
) -> bool:
    """Drop the candidate's enrolled voice profile via the gatekeeper's
    DELETE /enrolments/{uid} — the same HTTP seam enrolment rides, since the
    gatekeeper owns voice_embeddings writes. Idempotent and best-effort:
    deleting an absent profile is not an error, and a gatekeeper that is down
    or unconfigured must not make the deny path raise (the local row is still
    closed). Returns True only if a profile was actually removed."""
    if not gatekeeper_url or not _UID_RE.match(uid):
        return False
    base = gatekeeper_url.rstrip("/")
    headers = {}
    if gatekeeper_token:
        headers["Authorization"] = f"Bearer {gatekeeper_token}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.delete(
                f"{base}/enrolments/{uid}", headers=headers
            ) as resp:
                try:
                    body = await resp.json()
                except aiohttp.ContentTypeError:
                    return False
        return bool(body.get("removed"))
    except aiohttp.ClientError:
        return False


def _voice_profile_bound(db_path: str, uid: str) -> bool:
    """True if a voice_embeddings row exists for the uid (the onboarding
    enrolment, #386). The table is keyed by uid, so a present row *is* the
    binding — there is nothing to re-key."""
    if not Path(db_path).exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM voice_embeddings WHERE uid = ?", (uid,)
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def build_onboarding_approval_tools(
    db_path: str,
    sb_mcp_url: str,
    sb_mcp_token_path: str,
    gatekeeper_url: str = "",
    gatekeeper_token: str = "",
) -> list[Tool]:
    async def file_approval(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        pending = pending_residents_store.get_pending_by_uid(db_path, uid)
        if pending is None:
            return json.dumps({"ok": False, "reason": "no_pending_request"})
        if pending.get("request_id"):
            return json.dumps(
                {"ok": True, "request_id": pending["request_id"], "status": "filed"}
            )

        filed = json.loads(
            await call_sb_tool(
                sb_mcp_url,
                sb_mcp_token_path,
                "file_access_request",
                {
                    "subject": pending["display_name"],
                    "kind": "resident",
                    "username": uid,
                    "payload": "Solaris resident onboarding — voice profile enrolled.",
                    "requested_by": "solaris-onboarding",
                },
            )
        )
        request_id = filed.get("id") or filed.get("request_id")
        if not request_id:
            return json.dumps({"ok": False, "reason": "file_failed", "detail": filed})
        pending_residents_store.set_request_id(db_path, pending["id"], str(request_id))
        return json.dumps(
            {"ok": True, "request_id": str(request_id), "status": "filed"}
        )

    async def check_approval(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        pending = pending_residents_store.get_pending_by_uid(db_path, uid)
        if pending is None:
            return json.dumps({"ok": False, "reason": "no_pending_request"})
        request_id = pending.get("request_id")
        if not request_id:
            return json.dumps({"ok": False, "reason": "not_filed"})

        polled = json.loads(
            await call_sb_tool(
                sb_mcp_url,
                sb_mcp_token_path,
                "get_access_request_status",
                {"id": str(request_id)},
            )
        )
        status = polled.get("status")
        if status == "approved":
            pending_residents_store.mark_approved(db_path, pending["id"])
            return json.dumps(
                {
                    "ok": True,
                    "status": "approved",
                    "provisioned": True,
                    "uid": uid,
                    "voice_profile_bound": _voice_profile_bound(db_path, uid),
                }
            )
        if status in ("denied", "not-found"):
            # Provision nothing and drop the captured biometrics: a denied
            # candidate (or a request SB no longer knows) must leave no
            # resident, no account and no embedding behind. Delete first, then
            # close the local row — both are idempotent, so a re-poll is safe.
            removed = await _delete_voice_profile(gatekeeper_url, gatekeeper_token, uid)
            pending_residents_store.mark_denied(db_path, pending["id"])
            return json.dumps(
                {
                    "ok": True,
                    "status": status,
                    "provisioned": False,
                    "uid": uid,
                    "biometric_dropped": removed,
                }
            )
        # pending (or any unexpected status) → no provisioning, surface it.
        return json.dumps({"ok": True, "status": status, "provisioned": False})

    return [
        Tool(
            name="file_resident_approval",
            description=(
                "Reicht eine ausstehende Bewohner-Registrierung zur Freigabe in"
                " der zentralen ServiceBay-Anfrageliste ein (Onboarding,"
                " admin-only). uid der Kandidat:in. Gibt die Anfrage-id zurück;"
                " das Konto entsteht erst, wenn der Admin dort freigibt."
            ),
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=file_approval,
            visibility=Visibility.CONFIDENTIAL,
        ),
        Tool(
            name="check_resident_approval",
            description=(
                "Prüft den Freigabe-Status einer eingereichten"
                " Bewohner-Registrierung (admin-only). Bei Freigabe schließt es"
                " die Solaris-Seite ab: markiert die Anfrage als freigegeben und"
                " bestätigt das gebundene Sprachprofil. Bei Ablehnung wird nichts"
                " provisioniert und das erfasste Sprachprofil gelöscht. Solaris"
                " gibt nie selbst frei."
            ),
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=check_approval,
            visibility=Visibility.CONFIDENTIAL,
        ),
    ]
