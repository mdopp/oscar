"""Radio tool — play a resident's favorite station, ask + store it if unknown.

`play_radio` resolves a station NAME to a playable stream via radio-browser.info
(a free, keyless community index) and casts it through the same scoped
`media_player.play_media` path play_music uses (#604), so the engine never holds
a client-side HA token. The favorite is a deterministic per-user note at
`users/<uid>/preferences/radio-favorit.md` (frontmatter name + stream_url) — read
and written only for the calling resident, never another's (#576). "Spiele Radio"
with no station plays the stored favorite; an unknown favorite returns
`no_favorite` so the model can ask and call back with `station=<answer>`, which
stores it and plays.

Defensive by design: a radio-browser shape mismatch or an unreachable mirror
returns None (no match) rather than crashing — the real-box /verify confirms live
reachability.
"""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.ha import call_service_scoped

_MIRROR = "https://de1.api.radio-browser.info"
_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Dialog lines the model speaks VERBATIM (the register.py `say` pattern, #404):
# on gemma4:e4b prose steering is high-variance, so the two ask-for-more reasons
# carry the exact question here. Both MUST end in `?` — the facade re-opens the
# mic only on a trailing question mark (_question_pending).
_SAY_NEED_DEFAULT_DEVICE = "Auf welchem Gerät soll ich standardmäßig spielen?"
_SAY_NO_FAVORITE = "Welcher ist dein Lieblingssender?"

# A Cast GROUP rejects URL play_media with an HA 500 (#638); when a play on such
# a target fails we retry on a single device in the same area, but only on a
# 500-class server error — not on a normal not-found / need_device. Cap the
# fallback attempts so we never start an audible multi-cast storm.
_FALLBACK_MAX = 2


def is_cast_500(result: dict[str, Any]) -> bool:
    """Whether a play_media result is a 500-class HA failure (#638).

    call_service_scoped surfaces an HA error as {ok:false, error:'HA <status>:
    …'}; a 5xx means the server rejected the cast (a Cast group can't play a
    URL), which is what the area fallback retries — a 4xx (bad request) or a
    transport error is NOT retried on another speaker."""
    if result.get("ok"):
        return False
    error = str(result.get("error") or "")
    m = re.match(r"HA (\d{3})\b", error)
    return m is not None and 500 <= int(m.group(1)) < 600


async def cast_with_fallback(cast, entity_id, area_fallback):
    """Cast on `entity_id`; on a 500-class failure retry once per same-area device.

    `cast(target) -> result` performs the actual play_media (with its own bounded
    flakiness retry); `area_fallback(entity_id) -> list[str]` yields the other
    media_players in the same area, best candidate first (the room's Voice PE /
    esphome single speaker preferred). Returns (result, used_entity_id): on a
    fallback success `used_entity_id` is the device that actually played; when no
    candidate is found or none succeeds, the ORIGINAL failure is returned honestly
    (no fake success). At most `_FALLBACK_MAX` candidates are tried — one cast
    each, no unbounded loop / multi-cast storm."""
    result = await cast(entity_id)
    if result.get("ok") or not is_cast_500(result):
        return result, entity_id
    original = result
    candidates = (await area_fallback(entity_id)) if area_fallback is not None else []
    for target in candidates[:_FALLBACK_MAX]:
        alt = await cast(target)
        if alt.get("ok"):
            return alt, target
    return original, entity_id


def _user_agent() -> str:
    try:
        return f"Solaris/{version('solaris-chat')}"
    except PackageNotFoundError:
        return "Solaris/0"


class StationResolver(Protocol):
    async def resolve_station(self, name: str) -> tuple[str, str] | None: ...


class RadioBrowserClient:
    """Resolve a station name to (canonical_name, stream_url) via radio-browser.info."""

    def __init__(self, mirror: str = _MIRROR) -> None:
        self._mirror = mirror.rstrip("/")

    async def resolve_station(self, name: str) -> tuple[str, str] | None:
        query = name.strip()
        if not query:
            return None
        url = (
            f"{self._mirror}/json/stations/search"
            f"?name={quote(query)}&order=votes&reverse=true&limit=5"
        )
        headers = {"User-Agent": _user_agent()}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.get(url, headers=headers) as resp:
                    if resp.status >= 400:
                        return None
                    stations = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        if not isinstance(stations, list):
            return None
        playable = [
            s
            for s in stations
            if isinstance(s, dict) and str(s.get("url_resolved") or "").strip()
        ]
        if not playable:
            return None
        best = max(playable, key=lambda s: s.get("votes") or 0)
        return str(best.get("name") or query), str(best["url_resolved"]).strip()


def _sanitize_field(value: str) -> str:
    """Strip newlines / control chars so a value can't inject extra frontmatter."""
    return "".join(c for c in value if c == " " or c.isprintable()).strip()


def _pref_path(notes_dir: str, uid: str, name: str) -> Path:
    """A resident-scoped preference note (`users/<uid>/preferences/<name>.md`).

    Read and written only for the calling resident (#576) — caller B never sees
    resident A's preference."""
    return Path(notes_dir) / "users" / uid / "preferences" / f"{name}.md"


def _read_pref(
    notes_dir: str, uid: str, name: str, keys: tuple[str, ...]
) -> dict[str, str]:
    """Frontmatter fields of a resident's preference note, limited to `keys`."""
    path = _pref_path(notes_dir, uid, name)
    if not path.is_file():
        return {}
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in keys:
            fields[key.strip()] = value.strip()
    return fields


def _write_pref(notes_dir: str, uid: str, name: str, fields: dict[str, str]) -> None:
    """Write a resident's preference note as sanitized frontmatter. A pref with
    no real values isn't persisted — that would leave a bare `---\\n---\\n` shell
    (e.g. resolving a default device before one is set); drop any stale stub."""
    if not notes_dir:
        # No notes root configured — a relative _pref_path would write the note
        # into the process CWD instead of the resident's space.
        return
    sanitized = {k: _sanitize_field(v) for k, v in fields.items()}
    path = _pref_path(notes_dir, uid, name)
    if not any(v.strip() for v in sanitized.values()):
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}: {v}\n" for k, v in sanitized.items())
    path.write_text(f"---\n{body}---\n", encoding="utf-8")


def _read_favorite(notes_dir: str, uid: str) -> dict[str, str] | None:
    fields = _read_pref(notes_dir, uid, "radio-favorit", ("name", "stream_url"))
    if "name" in fields and "stream_url" in fields:
        return {"name": fields["name"], "stream_url": fields["stream_url"]}
    return None


def _write_favorite(notes_dir: str, uid: str, name: str, url: str) -> None:
    _write_pref(notes_dir, uid, "radio-favorit", {"name": name, "stream_url": url})


def _read_default_device(notes_dir: str, uid: str) -> str | None:
    """The resident's stored default playback device (entity_id), None if unset."""
    entity_id = _read_pref(
        notes_dir, uid, "default-device", ("entity_id", "label")
    ).get("entity_id", "")
    return entity_id or None


def _write_default_device(notes_dir: str, uid: str, entity_id: str) -> None:
    _write_pref(notes_dir, uid, "default-device", {"entity_id": entity_id})


def resolve_play_device(
    notes_dir: str,
    uid: str,
    entity_id: str,
    *,
    room: str = "",
    resolved_room_device: str = "",
) -> tuple[str, str | None]:
    """The target media_player for a play call + the reason when none resolves.

    Precedence (option C, #622): an explicitly-named device > the originating
    room's device (u99) > the resident's stored default. With no device and no
    room, the stored default is used; with none of these the reason is
    `need_default_device` so the model asks for one. When an explicit device is
    named and the resident has no default yet, that first device is STORED as the
    default (the one-off case — a default already exists — never overwrites it).
    """
    if entity_id:
        if notes_dir and _read_default_device(notes_dir, uid) is None:
            _write_default_device(notes_dir, uid, entity_id)
        return entity_id, None
    if notes_dir and room:
        room_prefs = _read_pref(
            notes_dir,
            uid,
            "room-devices",
            (room, room.lower(), room.capitalize(), room.title()),
        )
        for v in room_prefs.values():
            if v.strip():
                return v.strip(), None
    if room and resolved_room_device:
        return resolved_room_device, None
    if notes_dir:
        stored = _read_default_device(notes_dir, uid)
        if stored:
            return stored, None
    return "", "need_default_device"


def build_radio_tools(
    notes_dir: str,
    hass_url: str,
    hass_token: str,
    uid_getter,
    *,
    room_getter=None,
    room_resolver=None,
    area_fallback=None,
) -> list[Tool]:
    resolver: StationResolver = RadioBrowserClient()

    async def play_radio(args: dict[str, Any]) -> str:
        entity_id = str(args.get("entity_id") or "").strip()
        station = str(args.get("station") or "").strip()
        caller = uid_getter()
        # Precedence (option C, #622): explicit device > current room (u99) >
        # stored per-user default > ask (need_default_device). A first explicit
        # device with no default yet is stored as the default.
        room = room_getter() if (not entity_id and room_getter) else ""
        room_device = (
            (await room_resolver(room)) or ""
            if room and room_resolver is not None
            else ""
        )
        entity_id, reason = resolve_play_device(
            notes_dir,
            caller,
            entity_id,
            room=room,
            resolved_room_device=room_device,
        )
        if reason is not None:
            return json.dumps(
                {"ok": False, "reason": reason, "say": _SAY_NEED_DEFAULT_DEVICE},
                ensure_ascii=False,
            )

        if station:
            import re

            _RADIO_VERBS = re.compile(
                r"^(spiel(e|t)?|play|starte?|mach(e|t)?|schalt(e|en)?|ich\s+möchte|kannst?\s+du(\s+bitte)?|lass|bitte)\s+",
                re.I,
            )
            _ROOM_SUFFIXES = re.compile(
                r"\s+(im|in|auf)\s+(der|dem|den)?\s*(wohnzimmer|küche|kueche|kinderzimmer|bad|badezimmer|schlafzimmer|büro|buero|flur|garten)\b",
                re.I,
            )
            _ACTION_PARTICLES = re.compile(
                r"\s+(an|ein|ab|laufen|spielen|bitte|jetzt)\b", re.I
            )
            _RADIO_NORMALISE = {
                "einslive": "1live",
                "eins live": "1live",
                "1live": "1live",
                "1 live": "1live",
                "einsleif": "1live",
                "eins leif": "1live",
                "ein slive": "1live",
                "eins lief": "1live",
                "da ins live": "1live",
                "spiel da ins live": "1live",
                "ins live": "1live",
                "eins life": "1live",
                "1 life": "1live",
                "einslaiv": "1live",
                "1laiv": "1live",
                "ndr2": "ndr 2",
                "ndr 2": "ndr 2",
                "wdr2": "wdr 2",
                "wdr 2": "wdr 2",
            }
            clean_station = re.sub(r"[^\w\s]", "", station.casefold()).strip()
            clean_station = _RADIO_VERBS.sub("", clean_station).strip()
            clean_station = _ACTION_PARTICLES.sub("", clean_station).strip()
            clean_station = _ROOM_SUFFIXES.sub("", clean_station).strip()
            clean_station = _RADIO_NORMALISE.get(clean_station, clean_station)
            # Try the station as given first (an exact name resolves directly);
            # only fall back to the voice-cleaned form when it doesn't, so a
            # transcribed "spiel 1live im wohnzimmer" still lands on "1live".
            resolved = await resolver.resolve_station(station)
            if resolved is None and clean_station and clean_station != station:
                resolved = await resolver.resolve_station(clean_station)
            if resolved is None:
                return json.dumps(
                    {"ok": False, "reason": "station_not_found", "query": station},
                    ensure_ascii=False,
                )
            name, url = resolved
            _write_favorite(notes_dir, caller, name, url)
        else:
            fav = _read_favorite(notes_dir, caller)
            if fav is None:
                return json.dumps(
                    {"ok": False, "reason": "no_favorite", "say": _SAY_NO_FAVORITE},
                    ensure_ascii=False,
                )
            name, url = fav["name"], fav["stream_url"]

        async def _cast(target: str) -> dict[str, Any]:
            return await call_service_scoped(
                hass_url,
                hass_token,
                target,
                "media_player.play_media",
                {"media_content_type": "music", "media_content_id": url},
            )

        # A Cast GROUP 500s on URL play_media (#638); on that, retry once on a
        # single device in the same area (the room's Voice PE preferred).
        result, used = await cast_with_fallback(_cast, entity_id, area_fallback)
        if not result.get("ok"):
            return json.dumps(
                {
                    "ok": False,
                    "reason": "play_failed",
                    "detail": result.get("error"),
                    "station": name,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "station": name,
                "entity_id": used,
                "played": True,
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="play_radio",
            description=(
                "Spielt den Lieblings-Radiosender auf einem Raum-Gerät. 'Spiele"
                " Radio' ⇒ ohne Argumente (gespeicherter Lieblingssender). station"
                " NUR setzen, wenn der Nutzer einen Sender nennt — das löst ihn"
                " auf, speichert ihn dauerhaft und spielt. entity_id NUR bei"
                " genanntem Gerät/Raum. Liefert das Ergebnis 'say', sprich diese"
                " Zeile wörtlich und rufe mit der Antwort erneut auf (Sendername ⇒"
                " station, Gerät ⇒ entity_id). Bestätige nur den zurückgegebenen"
                " Sendernamen. NUR Radio — Musik: play_music, Podcasts:"
                " media_find_podcast."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "station": {"type": "string"},
                    "entity_id": {"type": "string"},
                },
            },
            handler=play_radio,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
