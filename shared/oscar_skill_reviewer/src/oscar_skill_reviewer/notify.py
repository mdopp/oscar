"""Send the post-apply Signal DM to the admin.

Why HTTP and not a direct signal-cli call: we already have an in-pod
`signal-gateway` (PR-A) with `POST /send` + bearer auth + retry. No
reason to duplicate that here.
"""

from __future__ import annotations

import httpx
from oscar_logging import log


async def notify_admin_via_signal(
    *,
    signal_url: str,
    signal_token: str,
    admin_number: str,
    skill_name: str,
    diff: str,
    reason: str,
    timeout: float = 10.0,
) -> None:
    """POST to signal-gateway. Failures log + bubble — caller decides next step."""
    text = (
        "📝 OSCAR hat sich verbessert.\n"
        f"Skill: {skill_name}\n"
        f"Grund: {reason}\n\n"
        f"Diff:\n```diff\n{_truncate(diff, 2500)}\n```\n\n"
        f"`/revert {skill_name}` zum Zurücknehmen."
    )
    headers = {"Content-Type": "application/json"}
    if signal_token:
        headers["Authorization"] = f"Bearer {signal_token}"
    payload = {"to": admin_number, "text": text}

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{signal_url.rstrip('/')}/send", json=payload, headers=headers
        )
    if response.status_code >= 400:
        log.error(
            "skill_reviewer.notify.error",
            skill=skill_name,
            status=response.status_code,
            body=response.text[:200],
        )
        response.raise_for_status()
    log.info("skill_reviewer.notify.ok", skill=skill_name, admin=admin_number)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…[truncated]"
