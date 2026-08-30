"""Household notices from Home Assistant — the addressing side (#1276).

HA automations today call `notify.mobile_app_<device>`, which wires *a phone*
into the automation: every new person and every new phone means editing every
affected automation, and "who should see this" ends up scattered across HA.
Solaris already knows which residents exist and which of their devices are
paired, so the decision belongs here. An automation says only **what happened**
and **who it concerns**; this module turns that into the set of devices.

## NOT AN ALARM CHANNEL — read this before routing anything through it

Delivery is **best effort and always will be**. A notice reaches an open client
immediately over the `/napi/portal/events` SSE stream, and a backgrounded
browser PWA through Web Push; a phone that is unreachable gets it whenever it
next reconnects, or not at all. Nothing here retries, queues, escalates, or
rings. `urgency` changes how the phone *presents* a notice, never whether it
arrives — there is deliberately no level that makes this channel safe for a
smoke detector, an intrusion, a medical event or anything else that must wake
somebody. Those need a real alerting transport, which is a separate decision and
does not exist yet. `NOT_AN_ALARM` is echoed in every response so the statement
reaches whoever is writing the automation, not just whoever reads this file.

## Targeting is the privacy surface

`target` is a resident **uid** (the id `/api/whoami` reports and `@person` uses)
or the household group. A resident is "known" when they have at least one paired
device — a device token or a Web Push subscription — which is precisely the set
the fan-out can reach. Matching is exact, case-insensitively, and nothing else:
no prefix, no fuzzy, no nearest match. An unknown or misspelled target resolves
to **nothing** and is refused, because the alternative failure mode is a notice
meant for one resident being read by the whole house.

The payload key set is closed, like the model lease (#1260) — an unknown field
is refused rather than ignored, so the contract with HA (and with the app,
`docs/features/companion-api.md`) cannot drift by accident.
"""

from __future__ import annotations

from typing import Any

from solaris_chat import device_token_store, push_store

# The SSE/bus event kind, the third alongside `card_state` and `servicebay`
# (consumer: mdopp/solaris-android#116). Same bus, same stream, one more kind —
# not a parallel notification mechanism.
EVENT_KIND = "ha"

# What every response repeats to the caller. The one line an automation author
# has to read before they trust this with something that matters.
NOT_AN_ALARM = (
    "best-effort: not an alarm channel — this does not wake a phone and must "
    "never carry smoke, intrusion or anything safety-critical"
)

# The complete payload. Adding a key here is a contract change on both sides.
PAYLOAD_KEYS = ("target", "title", "body", "urgency", "actions")
# One action, as the app maps it onto its existing WidgetActionActivity path
# (confirmation dialog + the server-side `sensitive_action` gate) — a second
# action route would be redundant and the weaker choice security-wise.
ACTION_KEYS = ("action", "title")

# Presentation only — see the "not an alarm channel" note above.
URGENCY_LEVELS = ("low", "normal", "high")
DEFAULT_URGENCY = "normal"

MAX_ACTIONS = 3
MAX_TITLE = 120
MAX_BODY = 500


def _parse_actions(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ACTIONS:
        raise ValueError("invalid_actions")
    actions: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - set(ACTION_KEYS):
            raise ValueError("invalid_actions")
        action, title = item.get("action"), item.get("title")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("invalid_actions")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("invalid_actions")
        actions.append({"action": action.strip(), "title": title.strip()})
    return actions


def parse_payload(body: Any) -> dict[str, Any]:
    """The normalised notice from a request body, or `ValueError(<reason>)`."""
    if not isinstance(body, dict):
        raise ValueError("invalid_payload")
    if set(body) - set(PAYLOAD_KEYS):
        raise ValueError("unexpected_field")
    target = body.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("invalid_target")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("invalid_title")
    text = body.get("body", "")
    if not isinstance(text, str):
        raise ValueError("invalid_body")
    urgency = body.get("urgency", DEFAULT_URGENCY)
    if urgency not in URGENCY_LEVELS:
        raise ValueError("invalid_urgency")
    return {
        "target": target.strip(),
        "title": title.strip()[:MAX_TITLE],
        "body": text.strip()[:MAX_BODY],
        "urgency": urgency,
        "actions": _parse_actions(body.get("actions")),
    }


def known_uids(db_path: str) -> set[str]:
    """Residents with at least one paired device — the addressable set.

    The union of the two device stores: a native device token (the app) and a
    Web Push subscription (the browser PWA). A resident with neither is not
    addressable, which is honest — there is nothing to deliver to.
    """
    return push_store.list_owner_uids(db_path) | device_token_store.list_owner_uids(
        db_path
    )


def resolve(
    db_path: str, target: str, *, household_uid: str = "household"
) -> tuple[str, list[str]] | None:
    """`(bus_uid, push_uids)` for a target, or **None** when nothing matches.

    `bus_uid` is the event-bus stream the notice is published on; `push_uids`
    are the residents whose backgrounded PWA gets a Web Push. Naming the
    household group publishes once on the shared household stream (every open
    client already subscribes to it) and pushes to every known resident.

    None means "do not deliver anything" — never "deliver to everyone".
    """
    name = (target or "").strip().casefold()
    if not name:
        return None
    uids = known_uids(db_path)
    if name == household_uid.strip().casefold():
        return household_uid, sorted(uids)
    for uid in sorted(uids):
        if uid.casefold() == name:
            return uid, [uid]
    return None
