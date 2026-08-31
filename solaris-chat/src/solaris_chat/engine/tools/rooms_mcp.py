"""The gatekeeper's room MCP as a household toolbox (#1295).

`set_room` / `list_rooms` live in the gatekeeper (`gatekeeper/mcp_server.py`)
behind their own loopback listener, so the engine can remap a satellite without
holding the gatekeeper's `PUSH_TOKEN` (which also opens `/push`). This is the
client half: a second `McpToolbox` on the household profile, carrying
`GATEKEEPER_MCP_TOKEN` instead of the SB-MCP token file.

Who may WRITE a room mapping is decided here, in code, not in the prompt:

* The guest profile (#353) never gets this toolbox — a speaker that speaker-ID
  actively resolved as UNKNOWN cannot dispatch what its toolbox doesn't hold.
* An unidentified turn that still lands on household (speaker-ID on, this
  utterance unmatched) is neither offered nor allowed `set_room`. The predicate
  is the PERSONAL visibility gate's (#1130/#1152), collapse included: where
  speaker-ID is off for the whole install nothing can ever match, so gating
  there would mean nobody could ever answer "which room is this?" out loud.

`list_rooms` is a HOUSEHOLD-class read — satellite ids and room names, no
resident data — so it stays available wherever the toolbox is registered.
"""

from __future__ import annotations

import json
from typing import Any

from solaris_chat.engine.tools import (
    CHANNEL_VOICE,
    current_channel,
    current_speaker_id_enabled,
    current_speaker_matched,
)
from solaris_chat.engine.tools.mcp_tools import McpToolbox

WRITE_TOOL = "set_room"

_SAY_UNIDENTIFIED = (
    "Ich bin mir nicht sicher, wer gerade spricht — den Raum eines Lautsprechers"
    " ändere ich dann nicht. In der Solaris-App kannst du ihn setzen."
)


def _may_set_room() -> bool:
    """Whether this turn may remap a satellite's room.

    Off the voice path the turn sits behind the SSO session, so it may. On the
    voice path it may only when speaker-ID matched this utterance to a resident
    — or when speaker-ID is off for the install, where the class collapses
    (#1130) because no turn can ever satisfy it."""
    if current_channel.get() != CHANNEL_VOICE:
        return True
    return current_speaker_matched.get() or not current_speaker_id_enabled.get()


class RoomMcpToolbox(McpToolbox):
    """The gatekeeper's room MCP, bearer-authenticated from an env token."""

    def __init__(self, url: str, token: str) -> None:
        super().__init__(url, "")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def definitions(self) -> list[dict[str, Any]]:
        defs = super().definitions()
        if _may_set_room():
            return defs
        return [d for d in defs if d["function"]["name"] != WRITE_TOOL]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == WRITE_TOOL and not _may_set_room():
            return json.dumps(
                {
                    "ok": False,
                    "reason": "unidentified_speaker",
                    "say": _SAY_UNIDENTIFIED,
                },
                ensure_ascii=False,
            )
        return await super().dispatch(name, arguments)
