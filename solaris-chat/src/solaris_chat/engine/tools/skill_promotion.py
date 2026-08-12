"""Dynamic-skill promotion via the generic ServiceBay approval API (#427).

The dynamic-skills skill drafts a new skill into the *pending* directory
(`<SKILLS_DIR>/_pending/<slug>/SKILL.md`); it never goes live until a human
approves it. ServiceBay 4.117.0 (#1818) ships a generic, service-agnostic
approval API, so promotion is the same file→poll shape the resident-onboarding
flow uses (`onboarding_approval.py`):

  file_skill_approval(slug)  → files a pending approval request onto SB's
      central access-request list via file_access_request, then records the
      returned request id in `_pending/<slug>/.request_id`. SB knows nothing
      skill-specific — it only holds "a request from solaris-skills awaiting
      approval". The admin approves once in the SB dashboard.

  check_skill_approval(slug) → polls that request id via
      get_access_request_status (pending / approved / denied / not-found). On
      "approved" the engine promotes the skill itself: it moves
      `_pending/<slug>` → `<SKILLS_DIR>/<slug>`. The household skill pack is
      read fresh per use (the panel lists it live; crons read a skill body by
      id per run), so the move *is* the reload — no service restart, and nothing
      relies on ServiceBay moving files. On "denied"/"not-found" the pending
      draft is deleted. The admin is the gate; Solaris never approves itself.

Admin-only, like the onboarding-approval tools — a household/guest turn drafts
into pending (the dynamic-skills skill) but can never file or check an approval.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.mcp_tools import call_sb_tool

# Lowercase letters/digits/dashes; no path separators, no leading dot — the same
# safe-slug shape the dynamic-skills skill enforces when it writes the draft, so
# a slug can never escape the pending dir.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_PENDING_SUBDIR = "_pending"
_REQUEST_ID_FILE = ".request_id"


def _contained(root: Path, child: Path) -> bool:
    """True only if ``child`` resolves to a path strictly inside ``root``.

    The slug regex already blocks ``/``, ``..``, leading dots and whitespace, but
    a symlinked pending dir (planted by a compromised draft step) could still make
    ``pending_root / slug`` resolve outside the skills sandbox. Resolving real
    paths and re-checking containment closes that escape before any move.
    """
    try:
        resolved = child.resolve(strict=False)
    except OSError:
        return False
    root_resolved = root.resolve(strict=False)
    return resolved == root_resolved or root_resolved in resolved.parents


def build_skill_promotion_tools(
    skills_dir: str,
    sb_mcp_url: str,
    sb_mcp_token_path: str,
) -> list[Tool]:
    active_root = Path(skills_dir)
    pending_root = active_root / _PENDING_SUBDIR

    async def file_approval(args: dict[str, Any]) -> str:
        slug = str(args.get("slug") or "").strip()
        if not _SLUG_RE.match(slug):
            return json.dumps({"ok": False, "reason": "invalid_slug"})
        pending_dir = pending_root / slug
        if not _contained(pending_root, pending_dir):
            return json.dumps({"ok": False, "reason": "path_escape"})
        if not (pending_dir / "SKILL.md").is_file():
            return json.dumps({"ok": False, "reason": "no_pending_skill"})

        request_file = pending_dir / _REQUEST_ID_FILE
        existing = (
            request_file.read_text(encoding="utf-8").strip()
            if request_file.is_file()
            else ""
        )
        if existing:
            return json.dumps({"ok": True, "request_id": existing, "status": "filed"})

        filed = json.loads(
            await call_sb_tool(
                sb_mcp_url,
                sb_mcp_token_path,
                "file_access_request",
                {
                    "subject": slug,
                    "kind": "skill",
                    "payload": f"Solaris dynamic skill draft '{slug}' awaiting promotion.",
                    "requested_by": "solaris-skills",
                },
            )
        )
        request_id = filed.get("id") or filed.get("request_id")
        if not request_id:
            return json.dumps({"ok": False, "reason": "file_failed", "detail": filed})
        request_file.write_text(str(request_id), encoding="utf-8")
        return json.dumps(
            {"ok": True, "request_id": str(request_id), "status": "filed"}
        )

    async def check_approval(args: dict[str, Any]) -> str:
        slug = str(args.get("slug") or "").strip()
        if not _SLUG_RE.match(slug):
            return json.dumps({"ok": False, "reason": "invalid_slug"})
        pending_dir = pending_root / slug
        if not _contained(pending_root, pending_dir):
            return json.dumps({"ok": False, "reason": "path_escape"})
        request_file = pending_dir / _REQUEST_ID_FILE
        if not request_file.is_file():
            return json.dumps({"ok": False, "reason": "not_filed"})
        request_id = request_file.read_text(encoding="utf-8").strip()
        if not request_id:
            return json.dumps({"ok": False, "reason": "not_filed"})

        polled = json.loads(
            await call_sb_tool(
                sb_mcp_url,
                sb_mcp_token_path,
                "get_access_request_status",
                {"id": request_id},
            )
        )
        status = polled.get("status")
        if status == "approved":
            active_dir = active_root / slug
            # Re-confirm containment at the move site: a symlinked pending dir
            # (or one made symlinked since filing) must never be transplanted
            # into the active pack, and the destination must stay inside the
            # active root. pending_dir must be a real directory, not a symlink.
            if (
                not _contained(active_root, active_dir)
                or not _contained(pending_root, pending_dir)
                or pending_dir.is_symlink()
                or not pending_dir.is_dir()
            ):
                return json.dumps({"ok": False, "reason": "path_escape"})
            if active_dir.exists():
                # Already promoted (a re-poll after a prior approval) — clean up
                # the now-stale pending draft so it stops surfacing.
                shutil.rmtree(pending_dir, ignore_errors=True)
                return json.dumps(
                    {"ok": True, "status": "approved", "promoted": True, "slug": slug}
                )
            request_file.unlink(missing_ok=True)
            shutil.move(str(pending_dir), str(active_dir))
            return json.dumps(
                {"ok": True, "status": "approved", "promoted": True, "slug": slug}
            )
        if status in ("denied", "not-found"):
            shutil.rmtree(pending_dir, ignore_errors=True)
            return json.dumps(
                {"ok": True, "status": status, "promoted": False, "slug": slug}
            )
        return json.dumps({"ok": True, "status": status, "promoted": False})

    return [
        Tool(
            name="file_skill_approval",
            description=(
                "Reicht einen ausstehenden Skill-Entwurf zur Freigabe in der"
                " zentralen ServiceBay-Anfrageliste ein (admin-only). slug = der"
                " Verzeichnisname des Entwurfs unter den ausstehenden Skills. Gibt"
                " die Anfrage-id zurück; der Skill geht erst live, wenn der Admin"
                " dort freigibt. Solaris gibt nie selbst frei."
            ),
            parameters={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
            handler=file_approval,
            visibility=Visibility.CONFIDENTIAL,
        ),
        Tool(
            name="check_skill_approval",
            description=(
                "Prüft den Freigabe-Status eines eingereichten Skill-Entwurfs"
                " (admin-only). Bei Freigabe schiebt Solaris den Entwurf selbst in"
                " den aktiven Skill-Ordner (kein Neustart nötig). Bei Ablehnung wird"
                " der Entwurf verworfen. Solaris gibt nie selbst frei."
            ),
            parameters={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
            handler=check_approval,
            visibility=Visibility.CONFIDENTIAL,
        ),
    ]
