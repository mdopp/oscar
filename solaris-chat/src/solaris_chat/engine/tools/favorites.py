"""The `pin_favorite` tool — pin a device or the last action to the start page.

One deterministic tool whose HANDLER decides entity-vs-action, so the model never
reconstructs arguments (code-side steering beats prompt steering, #645). With a
`target` it resolves the device and pins an entity card; without one it pins the
last real action of THIS conversation, read verbatim from the in-process trace
recorder (the exact args that ran — not a model re-guess). A held, confirm-gated
action is never recorded, so an unconfirmed sensitive action can't be pinned by
construction; a sensitive action that DID run is refused here too — a one-tap
start-page bypass of the confirmation gate must not exist.

`PINNABLE_TOOLS` lives here (client.py imports it for the usage counter); keep
this module import-clean of `engine.client` to avoid an import cycle.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from solaris_chat import favorites_store
from solaris_chat.engine import confirm
from solaris_chat.engine.registry import EntityRegistry
from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.ha import _SERVICE_ALIASES, resolve_entity_ref
from solaris_chat.engine.trace import TraceRecorder

# Tools whose executed calls may be pinned as a start-page action (and counted
# for "häufig genutzt"). Deliberately narrow: device control + media, never a
# read/discovery tool. Shared with client.py's usage counter.
PINNABLE_TOOLS = frozenset(
    {
        "ha_call_service",
        "ha_run_scene_script",
        "play_music",
        "play_radio",
        "media_find_podcast",
    }
)

_DESCRIPTION = (
    "Pinnt etwas auf die Startseite oder entfernt es ('packe das Bürolicht auf"
    " meine Startseite', 'nimm X von der Startseite'). target = das Gerät, WIE"
    " DER NUTZER ES NENNT; bei 'das'/'die letzte Aktion' direkt nach einer"
    " Aktion target WEGLASSEN — dann wird die letzte Aktion gepinnt."
    " scope='household' NUR bei 'unsere/die gemeinsame Startseite'. remove=true"
    " zum Entfernen. Gib die 'say'-Zeile der Antwort wörtlich aus."
)


def build_favorites_tools(
    db_path: str,
    uid_getter: Callable[[], str],
    session_getter: Callable[[], str],
    recorder: TraceRecorder,
    registry: EntityRegistry,
    hass_url: str,
    hass_token: str,
) -> list[Tool]:
    async def _sensitive_action(payload_args: dict[str, Any]) -> bool:
        """True when a to-be-pinned ha_call_service call is confirm-gated —
        mirrors client._gate_sensitive's normalization (service alias + a cover's
        device_class), failing SAFE (gated) when a class can't be resolved."""
        domain = str(payload_args.get("domain") or "")
        service = str(payload_args.get("service") or "")
        service = _SERVICE_ALIASES.get(domain, {}).get(service, service)
        device_class: str | None = None
        if domain == "cover":
            entity_id = str(payload_args.get("entity_id") or "")
            device_class = await registry.device_class(entity_id)
        return confirm.is_sensitive(domain, service, device_class)

    async def pin(args: dict[str, Any]) -> str:
        uid = uid_getter()
        default_uid = favorites_store.HOUSEHOLD
        scope = str(args.get("scope") or "")
        target = str(args.get("target") or "").strip()
        remove = bool(args.get("remove"))
        owner = (
            favorites_store.HOUSEHOLD
            if (scope == "household" or not uid or uid == default_uid)
            else uid
        )

        if remove:
            removed = 0
            if target:
                entity_id = await resolve_entity_ref(hass_url, hass_token, target)
                if entity_id:
                    removed = favorites_store.remove_by_entity(
                        db_path, owner, entity_id
                    )
            if removed:
                return json.dumps(
                    {
                        "ok": True,
                        "removed": removed,
                        "say": "Ist von der Startseite entfernt.",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": False,
                    "reason": "not_found",
                    "say": "Das habe ich auf deiner Startseite nicht gefunden.",
                },
                ensure_ascii=False,
            )

        if target:
            entity_id = await resolve_entity_ref(hass_url, hass_token, target)
            if not entity_id:
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "not_found",
                        "say": f"Welches Gerät meinst du mit „{target}“? Das finde ich nicht.",
                    },
                    ensure_ascii=False,
                )
            label = (
                entity_id.split(".", 1)[1].replace("_", " ")
                if "." in entity_id
                else entity_id
            )
            fav_id = favorites_store.add_favorite(
                db_path, owner, "entity", label, {"entity_id": entity_id}
            )
            return json.dumps(
                {
                    "ok": True,
                    "id": fav_id,
                    "say": f"„{label}“ ist jetzt auf deiner Startseite.",
                },
                ensure_ascii=False,
            )

        # No target: pin the last real action of THIS conversation.
        steps = recorder.for_session(session_getter(), 0.0)
        for step in reversed(steps):
            if step.get("step_kind") != "tool":
                continue
            name = step.get("tool_name")
            step_args = step.get("arguments")
            if name not in PINNABLE_TOOLS or not isinstance(step_args, dict):
                # Older records carry no arguments — can't reconstruct, skip.
                continue
            if name == "ha_call_service" and await _sensitive_action(step_args):
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "confirm_gated",
                        "say": "Diese Aktion ist bestätigungspflichtig und kann nicht"
                        " angepinnt werden.",
                    },
                    ensure_ascii=False,
                )
            label = _action_label(name, step_args)
            fav_id = favorites_store.add_favorite(
                db_path, owner, "action", label, {"tool": name, "args": step_args}
            )
            return json.dumps(
                {
                    "ok": True,
                    "id": fav_id,
                    "say": f"„{label}“ ist jetzt auf deiner Startseite.",
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "ok": False,
                "reason": "no_recent_action",
                "say": "Ich habe gerade keine Aktion, die ich anpinnen könnte."
                " Was möchtest du auf die Startseite legen?",
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="pin_favorite",
            description=_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "scope": {"type": "string", "enum": ["personal", "household"]},
                    "remove": {"type": "boolean"},
                },
            },
            handler=pin,
            visibility=Visibility.HOUSEHOLD,
        )
    ]


def _action_label(tool: str, args: dict[str, Any]) -> str:
    """A short human label for a pinned action favorite."""
    if tool == "ha_call_service":
        entity_id = str(args.get("entity_id") or "")
        name = (
            entity_id.split(".", 1)[1].replace("_", " ")
            if "." in entity_id
            else entity_id
        )
        service = str(args.get("service") or "")
        return f"{name} — {service}".strip(" —") or tool
    for key in ("query", "station", "title", "name"):
        val = args.get(key)
        if val:
            return str(val)
    return tool
