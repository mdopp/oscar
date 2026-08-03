"""Structured music-library query tool (#588).

Music questions ("welche Songs von <Künstler> habe ich") were answered by the
`notes_search` vault grep: a substring match (so "Queen" wrongly matched "Queens
of the Stone Age"), capped at 8 hits, and it leaked raw hash slugs. The
structured projection (`entities`/`facts`, populated by `ingest/jellyfin.py`) is
correct — a band is `entities.type='band'`, a song is `type='song'` with a
`facts(predicate='by', value='bands/<slug>')` edge — but no model tool read it.

This adds ONE token-lean `music_query` tool over that store:

  - `op="songs_by_artist"`: resolve the BAND — EXACT canonical_name (case-
    insensitive) first and it ALWAYS wins (Queen → Queen, never fuzzed to Queens
    of the Stone Age); only when nothing matches exactly does a RANKED FUZZY pass
    over the caller+household bands run (whole-word containment + typo edit-ratio,
    so "Joel" finds "Billy Joel" and "Beatls" finds "The Beatles", returning
    not-found below a threshold rather than a random band) — then follow its `by`
    edge to the songs, return clean `canonical_name` titles (never the hash
    slug), capped + a total.
  - `op="list_artists"`: the type='band' entities, optional name prefix.
  - `op="wishlist"`: "was anschaffen" — the acquire-list computed as a QUERY over
    album facts (ADR 0003/0004, #879), never a materialized note: `(wishlist OR
    used_to_love) AND NOT owned_physical AND NOT digitally-present`. Digital
    presence is the album-exists-via-Jellyfin signal — an `on_album` edge from a
    Jellyfin song (no separate `has_digital` fact). The producing facts land in
    P2b/P2c/P3 (#880/#881/#868), so this returns empty until they do.

Every query is per-owner scoped: `resident_uid IN (caller, 'household')` (caller
from `uid_getter`; an unknown/voice caller is `household`, so it sees only the
shared library — never another resident's private one).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Protocol

from solaris_chat.engine.fuzzy import (
    _FUZZY_PREFIX_BONUS,
    FUZZY_THRESHOLD,
    fuzzy_score,
)
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.ha import call_service_scoped
from solaris_chat.engine.tools.radio import (
    _SAY_NEED_DEFAULT_DEVICE,
    cast_with_fallback,
    resolve_play_device,
)


class LyricsClient(Protocol):
    """The slice of the Jellyfin client `song_lyrics`/`play_music` need (live)."""

    async def lyrics(self, audio_id: str) -> str | None: ...

    async def stream_url(self, audio_id: str, *, static: bool = True) -> str | None: ...

    async def playlists(self) -> list[tuple[str, str]]: ...

    async def create_playlist(self, name: str, ids: list[str]) -> str: ...

    async def playlist_add(self, playlist_id: str, ids: list[str]) -> bool: ...


_SONG_CAP = 50
_ARTIST_CAP = 50
_ALBUM_CAP = 50

# Cast play_media is intermittently flaky (#573 — structurally identical devices,
# one casts, the other returns play_failed), so retry a failing play a bounded
# number of times with a short backoff before surfacing the HA error.
_PLAY_RETRIES = 2
_PLAY_BACKOFF_S = 0.5

# The model stuffs filler ("ein Song von …", "Musik von …") into `title` instead
# of `artist` (#604). A title matching this is not a real title: capture the
# trailing name as the artist and clear the title.
_FILLER_TITLE_RE = re.compile(
    r"^(?:spiel(?:e)?\s+)?(?:mir\s+)?(?:ein(?:e|en)?\s+)?"
    r"(?:song|lied|musik|stück|stueck|etwas)\s+von\s+(?P<artist>.+)$",
    re.IGNORECASE,
)

# Fuzzy band-resolve weights/threshold (only reached when NO exact match exists).
# The scorer is shared with notes search; see `engine/fuzzy.py` for the blend.


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a model-supplied prefix stays a literal prefix.

    `%`/`_` in the arg would otherwise expand into wildcards, coercing the
    anchored-prefix LIKE back into a substring match (Queen → Queens-of-...).
    Used with `ESCAPE '\\'`; `\\` itself is escaped first."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _band_value(okf_path: str) -> str:
    """The `by`-fact value (`bands/<slug>`) a band's okf_path projects to.

    A band lives at `okf/bands/<slug>.md` (shared) or
    `users/<uid>/okf/bands/<slug>.md` (private); the song→band `by` edge stores
    the `okf/`-relative `bands/<slug>` (no owner prefix, no `.md`)."""
    rel = okf_path
    marker = "okf/"
    idx = rel.find(marker)
    if idx != -1:
        rel = rel[idx + len(marker) :]
    if rel.endswith(".md"):
        rel = rel[:-3]
    return rel


def _album_value(okf_path: str) -> str:
    """The `on_album`-fact value (`albums/<slug>`) an album's okf_path projects to.

    An album lives at `okf/albums/<slug>.md` (shared) or
    `users/<uid>/okf/albums/<slug>.md` (private); a song's `on_album` edge stores
    the `okf/`-relative `albums/<slug>` (no owner prefix, no `.md`) — same shape
    as `_band_value` for the `by` edge (#876)."""
    return _band_value(okf_path)


def _audio_id(resource: str) -> str:
    """The Jellyfin audio id from a song's `resource` fact
    (`jellyfin://audio/<id>` → `<id>`); empty when not a jellyfin audio URI."""
    prefix = "jellyfin://audio/"
    return resource[len(prefix) :] if resource.startswith(prefix) else ""


def _album_is_digital(conn, album_value: str, caller: str) -> bool:
    """Whether the album is digitally present — the ADR-0003 album-exists signal.

    True iff a Jellyfin-projected song links to it via `on_album → albums/<slug>`
    (`album_value`). Scoped to the caller (resident_uid IN (caller, household)) so
    another resident's digital copy never suppresses this caller's wishlist. No
    separate `has_digital` fact exists — presence of the on_album edge IS it."""
    row = conn.execute(
        "SELECT 1 FROM facts f JOIN entities e ON e.id = f.subject_entity_id"
        " WHERE f.predicate = 'on_album' AND f.value = ?"
        " AND e.type = 'song' AND e.resident_uid IN (?, ?) LIMIT 1",
        (album_value, caller, projection.SHARED_UID),
    ).fetchone()
    return row is not None


def _song_audio_id(conn, song_id: str, caller: str) -> str:
    """The Jellyfin audio id from a song's scoped `resource` fact, "" when none."""
    for f in projection.entity_facts(conn, song_id, caller):
        if f["predicate"] == "resource":
            return _audio_id(f["value"])
    return ""


def build_music_query_tools(
    db_path: str,
    uid_getter,
    jellyfin_client: LyricsClient | None = None,
    *,
    hass_url: str = "",
    hass_token: str = "",
    room_getter=None,
    room_resolver=None,
    area_fallback=None,
    notes_dir: str = "",
    recorder=None,
    session_getter=None,
) -> list[Tool]:
    def _caller() -> str:
        return uid_getter() or projection.SHARED_UID

    def _resolve_band_id(conn, artist: str, caller: str) -> str | None:
        # Prefer the shared exact resolver (id / exact canonical_name / alias),
        # but only accept it when it lands on a band — a person/place named the
        # same must not shadow the artist.
        ref = projection.resolve_entity_id(conn, artist, caller)
        if ref is not None:
            row = conn.execute(
                "SELECT id FROM entities WHERE id = ? AND type = 'band'", (ref,)
            ).fetchone()
            if row is not None:
                return row["id"]
        # Scoped exact (case-insensitive) on bands FIRST...
        row = conn.execute(
            "SELECT id FROM entities"
            " WHERE type = 'band' AND resident_uid IN (?, ?)"
            " AND canonical_name = ? COLLATE NOCASE"
            " ORDER BY canonical_name LIMIT 1",
            (caller, projection.SHARED_UID, artist),
        ).fetchone()
        if row is not None:
            return row["id"]
        # ...only then RANKED FUZZY over the caller+household bands (scoped fetch,
        # ranked in Python) so "Joel" finds "Billy Joel" and "Beatls" finds "The
        # Beatles" — but an exact match above already short-circuited, so "Queen"
        # never fuzzes to "Queens of the Stone Age". A fuzzy match must never
        # surface another resident's private band, hence the same resident scope.
        candidates = conn.execute(
            "SELECT id, canonical_name FROM entities"
            " WHERE type = 'band' AND resident_uid IN (?, ?)"
            " ORDER BY canonical_name",
            (caller, projection.SHARED_UID),
        ).fetchall()
        best_id: str | None = None
        best_score = 0.0
        for cand in candidates:
            score = fuzzy_score(artist, cand["canonical_name"])
            if score > best_score:
                best_score, best_id = score, cand["id"]
        return best_id if best_score >= FUZZY_THRESHOLD else None

    def _song_by_value(conn, song_id: str, caller: str) -> str | None:
        # The song's `by` edge value (`bands/<slug>`), scoped to the caller.
        row = conn.execute(
            "SELECT value FROM facts"
            " WHERE subject_entity_id = ? AND predicate = 'by'"
            " AND resident_uid IN (?, ?) LIMIT 1",
            (song_id, caller, projection.SHARED_UID),
        ).fetchone()
        return row["value"] if row is not None else None

    def _resolve_song_id(conn, title: str, artist: str, caller: str) -> str | None:
        # Exact canonical_name (case-insensitive) over the caller+household songs
        # first; only then a ranked fuzzy pass over the same scoped rows. When an
        # artist is given, prefer (among the exact, else among the top fuzzy ties)
        # the song whose `by` band matches that artist — so two songs sharing a
        # title disambiguate to the right artist's.
        want_value: str | None = None
        if artist:
            band_id = _resolve_band_id(conn, artist, caller)
            if band_id is not None:
                okf_path = projection.entity_okf_path(conn, band_id)
                if okf_path is not None:
                    want_value = _band_value(okf_path)
        exact = conn.execute(
            "SELECT id FROM entities"
            " WHERE type = 'song' AND resident_uid IN (?, ?)"
            " AND canonical_name = ? COLLATE NOCASE"
            " ORDER BY canonical_name",
            (caller, projection.SHARED_UID, title),
        ).fetchall()
        if exact:
            ids = [r["id"] for r in exact]
            if want_value is not None:
                for sid in ids:
                    if _song_by_value(conn, sid, caller) == want_value:
                        return sid
            return ids[0]
        query_first = title.split()[0] if title.split() else title
        candidates = conn.execute(
            "SELECT id, canonical_name FROM entities"
            " WHERE type = 'song' AND resident_uid IN (?, ?)"
            " AND (canonical_name LIKE ? OR canonical_name LIKE ?)"
            " ORDER BY canonical_name",
            (caller, projection.SHARED_UID, f"%{title}%", f"%{query_first}%"),
        ).fetchall()
        best_id: str | None = None
        best_score = 0.0
        for cand in candidates:
            score = fuzzy_score(title, cand["canonical_name"])
            # An artist match breaks fuzzy ties toward the right artist's song.
            if (
                want_value is not None
                and _song_by_value(conn, cand["id"], caller) == want_value
            ):
                score += _FUZZY_PREFIX_BONUS
            if score > best_score:
                best_score, best_id = score, cand["id"]
        return best_id if best_score >= FUZZY_THRESHOLD else None

    async def song_lyrics(title: str, artist: str) -> str:
        title = title.strip()
        artist = artist.strip()
        if not title:
            return json.dumps({"error": "title required"}, ensure_ascii=False)
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            song_id = _resolve_song_id(conn, title, artist, caller)
            if song_id is None:
                return json.dumps({"found": False}, ensure_ascii=False)
            song = projection.entity_row(conn, song_id)
            clean_title = song["canonical_name"]
            band_value = _song_by_value(conn, song_id, caller)
            artist_name = ""
            if band_value:
                rows = projection.fetch_all(
                    conn,
                    "SELECT e.canonical_name FROM concepts c"
                    " JOIN entities e ON e.id = c.ref_id"
                    " WHERE c.ref_kind = 'entity' AND e.type = 'band'"
                    " AND e.resident_uid IN (?, ?)"
                    " AND (c.okf_path LIKE '%okf/' || ? || '.md')",
                    (caller, projection.SHARED_UID, band_value),
                )
                if rows:
                    artist_name = rows[0]["canonical_name"]
            audio_id = _song_audio_id(conn, song_id, caller)
        finally:
            conn.close()
        lyrics = None
        if jellyfin_client is not None and audio_id:
            lyrics = await jellyfin_client.lyrics(audio_id)
        if lyrics:
            out: dict[str, Any] = {"title": clean_title, "lyrics": lyrics}
            if artist_name:
                out["artist"] = artist_name
            return json.dumps(out, ensure_ascii=False)
        result: dict[str, Any] = {
            "title": clean_title,
            "lyrics": None,
            "note": "keine Lyrics verfügbar",
        }
        if artist_name:
            result["artist"] = artist_name
        return json.dumps(result, ensure_ascii=False)

    async def songs_by_artist(artist: str, limit: int) -> str:
        artist = artist.strip()
        if not artist:
            return json.dumps({"error": "artist required"}, ensure_ascii=False)
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            band_id = _resolve_band_id(conn, artist, caller)
            if band_id is None:
                return json.dumps(
                    {"artist": artist, "total": 0, "songs": []}, ensure_ascii=False
                )
            band = projection.entity_row(conn, band_id)
            okf_path = projection.entity_okf_path(conn, band_id)
            if okf_path is None:
                return json.dumps(
                    {"artist": band["canonical_name"], "total": 0, "songs": []},
                    ensure_ascii=False,
                )
            value = _band_value(okf_path)
            rows = projection.fetch_all(
                conn,
                "SELECT e.canonical_name FROM facts f"
                " JOIN entities e ON e.id = f.subject_entity_id"
                " WHERE f.predicate = 'by' AND f.value = ?"
                " AND e.type = 'song' AND e.resident_uid IN (?, ?)"
                " ORDER BY e.canonical_name",
                (value, caller, projection.SHARED_UID),
            )
            titles = [r["canonical_name"] for r in rows]
            return json.dumps(
                {
                    "artist": band["canonical_name"],
                    "total": len(titles),
                    "songs": titles[:limit],
                },
                ensure_ascii=False,
            )
        finally:
            conn.close()

    async def artist_info(artist: str) -> str:
        artist = artist.strip()
        if not artist:
            return json.dumps({"error": "artist required"}, ensure_ascii=False)
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            band_id = _resolve_band_id(conn, artist, caller)
            if band_id is None:
                return json.dumps(
                    {"artist": artist, "found": False}, ensure_ascii=False
                )
            band = projection.entity_row(conn, band_id)
            # Caller-scoped facts (resident_uid IN (caller, 'household')) — a
            # private band's genre/bio never surfaces to another resident.
            facts = projection.entity_facts(conn, band_id, caller)
            by_predicate = {f["predicate"]: f["value"] for f in facts}
            okf_path = projection.entity_okf_path(conn, band_id)
            song_count = 0
            if okf_path is not None:
                value = _band_value(okf_path)
                rows = projection.fetch_all(
                    conn,
                    "SELECT COUNT(*) AS n FROM facts f"
                    " JOIN entities e ON e.id = f.subject_entity_id"
                    " WHERE f.predicate = 'by' AND f.value = ?"
                    " AND e.type = 'song' AND e.resident_uid IN (?, ?)",
                    (value, caller, projection.SHARED_UID),
                )
                song_count = rows[0]["n"]
            return json.dumps(
                {
                    "artist": band["canonical_name"],
                    "genre": by_predicate.get("genre", ""),
                    "bio": by_predicate.get("bio", ""),
                    "song_count": song_count,
                },
                ensure_ascii=False,
            )
        finally:
            conn.close()

    async def list_artists(prefix: str, limit: int) -> str:
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            sql = (
                "SELECT canonical_name FROM entities"
                " WHERE type = 'band' AND resident_uid IN (?, ?)"
            )
            params: list[Any] = [caller, projection.SHARED_UID]
            if prefix:
                sql += " AND canonical_name LIKE ? || '%' ESCAPE '\\' COLLATE NOCASE"
                params.append(_escape_like(prefix))
            sql += " ORDER BY canonical_name"
            rows = projection.fetch_all(conn, sql, tuple(params))
            names = [r["canonical_name"] for r in rows]
            return json.dumps(
                {"total": len(names), "artists": names[:limit]}, ensure_ascii=False
            )
        finally:
            conn.close()

    async def wishlist(limit: int) -> str:
        # "Was anschaffen": albums to acquire = (wishlist OR used_to_love) AND NOT
        # digitally present AND NOT owned_physical (ADR 0004). Digital presence is
        # the album-exists-via-Jellyfin signal (ADR 0003): an album is digitally
        # present iff a Jellyfin-projected song links to it via `on_album →
        # albums/<slug>` — no redundant `has_digital` fact is written. Per-owner
        # scoped (resident_uid IN (caller, household)) so one resident's wishlist
        # never leaks another's.
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            albums = conn.execute(
                "SELECT id, canonical_name FROM entities"
                " WHERE type = 'album' AND resident_uid IN (?, ?)"
                " ORDER BY canonical_name",
                (caller, projection.SHARED_UID),
            ).fetchall()
            acquire: list[str] = []
            for album in albums:
                preds = {
                    f["predicate"]
                    for f in projection.entity_facts(conn, album["id"], caller)
                }
                wanted = "wishlist" in preds or "used_to_love" in preds
                if not wanted or "owned_physical" in preds:
                    continue
                okf_path = projection.entity_okf_path(conn, album["id"])
                if okf_path is not None and _album_is_digital(
                    conn, _album_value(okf_path), caller
                ):
                    continue
                acquire.append(album["canonical_name"])
            return json.dumps(
                {"total": len(acquire), "albums": acquire[:limit]},
                ensure_ascii=False,
            )
        finally:
            conn.close()

    async def music_query(args: dict[str, Any]) -> str:
        op = str(args.get("op") or "").strip()
        limit = args.get("limit")
        if op == "wishlist":
            cap = int(limit) if isinstance(limit, int) and limit > 0 else _ALBUM_CAP
            return await wishlist(min(cap, _ALBUM_CAP))
        if op == "songs_by_artist":
            cap = int(limit) if isinstance(limit, int) and limit > 0 else _SONG_CAP
            return await songs_by_artist(
                str(args.get("artist") or ""), min(cap, _SONG_CAP)
            )
        if op == "list_artists":
            cap = int(limit) if isinstance(limit, int) and limit > 0 else _ARTIST_CAP
            return await list_artists(
                str(args.get("prefix") or "").strip(), min(cap, _ARTIST_CAP)
            )
        if op == "artist_info":
            return await artist_info(str(args.get("artist") or ""))
        if op == "song_lyrics":
            return await song_lyrics(
                str(args.get("title") or ""), str(args.get("artist") or "")
            )
        return json.dumps(
            {
                "error": "op must be songs_by_artist, list_artists,"
                " artist_info, song_lyrics or wishlist"
            },
            ensure_ascii=False,
        )

    def _band_first_castable(conn, artist: str, caller: str) -> tuple[str, str] | None:
        # The band's first song with a resolvable audio id: (canonical_name,
        # audio_id). Scoped to caller (resident_uid IN (caller, household)) —
        # another resident's private track is never enumerated.
        band_id = _resolve_band_id(conn, artist, caller)
        if band_id is None:
            return None
        okf_path = projection.entity_okf_path(conn, band_id)
        if okf_path is not None:
            value = _band_value(okf_path)
        else:
            band_row = projection.entity_row(conn, band_id)
            if band_row is None:
                return None
            from solaris_chat.engine.knowledge.okf import safe_slug

            value = f"bands/{safe_slug(band_row['canonical_name'])}"
        from solaris_chat.engine.knowledge.okf import safe_slug

        band_name = band_row["canonical_name"] if okf_path is None else value
        clean_slug = safe_slug(
            band_name.replace(", The", "").replace("The ", "").strip()
        )
        rows = projection.fetch_all(
            conn,
            "SELECT e.id, e.canonical_name FROM facts f"
            " JOIN entities e ON e.id = f.subject_entity_id"
            " WHERE f.predicate = 'by' AND (f.value = ? OR f.value LIKE ?)"
            " AND e.type = 'song' AND e.resident_uid IN (?, ?)"
            " ORDER BY e.canonical_name",
            (value, f"%{clean_slug}%", caller, projection.SHARED_UID),
        )
        for row in rows:
            audio_id = _song_audio_id(conn, row["id"], caller)
            if audio_id:
                return row["canonical_name"], audio_id
        return None

    def _random_castable(conn, caller: str) -> tuple[str, str] | None:
        # A random castable song from the caller's scope (resident_uid IN (caller,
        # household)) — never another resident's private track. One scoped query,
        # one pick (no retry loop): pick over the rows that already carry a
        # jellyfin audio resource, so the chosen song is always castable.
        rows = conn.execute(
            "SELECT e.id, e.canonical_name FROM entities e"
            " JOIN facts f ON f.subject_entity_id = e.id AND f.predicate = 'resource'"
            " WHERE e.type = 'song' AND e.resident_uid IN (?, ?)"
            " AND f.value LIKE 'jellyfin://audio/%'",
            (caller, projection.SHARED_UID),
        ).fetchall()
        if not rows:
            return None
        pick = random.choice(rows)
        audio_id = _song_audio_id(conn, pick["id"], caller)
        if not audio_id:
            return None
        return pick["canonical_name"], audio_id

    async def _cast_url(entity_id: str, url: str) -> dict[str, Any]:
        # Cast play_media is intermittently flaky (#573); retry a bounded number
        # of times with a short backoff and surface the last HA error on failure.
        result: dict[str, Any] = {}
        for attempt in range(_PLAY_RETRIES + 1):
            result = await call_service_scoped(
                hass_url,
                hass_token,
                entity_id,
                "media_player.play_media",
                {"media_content_type": "music", "media_content_id": url},
            )
            if result.get("ok"):
                return result
            if attempt < _PLAY_RETRIES:
                await asyncio.sleep(_PLAY_BACKOFF_S)
        return result

    async def _cast(entity_id: str, audio_id: str) -> dict[str, Any]:
        # Try the STATIC (direct/original-file) stream FIRST — no transcode, so a
        # Cast GROUP plays it where the /universal transcode 500s (#573/#604) — and
        # only fall back to the /universal (transcode) form if static play fails
        # (a container Cast can't play directly). Each form gets the bounded retry.
        result: dict[str, Any] = {}
        for static in (True, False):
            url = await jellyfin_client.stream_url(audio_id, static=static)
            if not url:
                continue
            result = await _cast_url(entity_id, url)
            if result.get("ok"):
                return result
        return result

    async def play_music(args: dict[str, Any]) -> str:
        title = str(args.get("title") or "").strip()
        artist = str(args.get("artist") or "").strip()
        # Treat generic filler words ('Musik', 'Spielermusik', 'Spiele Musik') as empty query
        if title.casefold() in {
            "musik",
            "spielermusik",
            "spiele musik",
            "spiel musik",
            "etwas musik",
            "radio",
        }:
            title = ""
        if artist.casefold() in {
            "musik",
            "spielermusik",
            "spiele musik",
            "spiel musik",
            "etwas musik",
            "radio",
        }:
            artist = ""
        # Redirect radio stations passed to play_music
        _RADIO_KEYWORDS = {
            "1live",
            "1 live",
            "einslive",
            "eins live",
            "ndr2",
            "ndr 2",
            "wdr2",
            "wdr 2",
            "ffn",
            "antenne niedersachsen",
            "radio bob",
            "dlf",
        }
        import re

        _RADIO_NORMALISE = {
            "einslive": "1 live",
            "eins live": "1 live",
            "1live": "1 live",
            "1 live": "1 live",
            "einsleif": "1 live",
            "eins leif": "1 live",
            "ein slive": "1 live",
            "eins lief": "1 live",
            "da ins live": "1 live",
            "spiel da ins live": "1 live",
            "ins live": "1 live",
            "eins life": "1 live",
            "1 life": "1 live",
            "einslaiv": "1 live",
            "1laiv": "1 live",
            "ndr2": "ndr 2",
            "ndr 2": "ndr 2",
            "wdr2": "wdr 2",
            "wdr 2": "wdr 2",
        }
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
        check_raw = re.sub(r"[^\w\s]", "", (title or artist).casefold()).strip()
        check_str = _RADIO_VERBS.sub("", check_raw).strip()
        check_str = _ACTION_PARTICLES.sub("", check_str).strip()
        check_str = _ROOM_SUFFIXES.sub("", check_str).strip()
        check_str = _RADIO_NORMALISE.get(check_str, check_str)
        _RADIO_PREFIXES = [
            "1 live",
            "ndr",
            "wdr",
            "swr",
            "hr",
            "br",
            "mdr",
            "rbb",
            "ffn",
            "antenne",
            "rock",
            "sunshine",
            "hit",
            "klassik",
            "schlager",
            "bigfm",
            "planet",
            "paloma",
            "brocken",
            "bob",
        ]
        is_radio_hint = (
            "radio" in check_str
            or "fm" in check_str
            or "sender" in check_str
            or check_str in _RADIO_KEYWORDS
            or any(k in check_str for k in _RADIO_PREFIXES)
        )
        if is_radio_hint:
            from solaris_chat.engine.tools.radio import build_radio_tools

            r_tools_local = build_radio_tools(
                notes_dir,
                hass_url,
                hass_token,
                _caller,
                room_getter=room_getter,
                room_resolver=room_resolver,
                area_fallback=area_fallback,
            )
            r_handler_local = next(
                (t.handler for t in r_tools_local if t.name == "play_radio"), None
            )
            if r_handler_local:
                res = await r_handler_local(
                    {"station": check_str, "entity_id": args.get("entity_id") or ""}
                )
                if json.loads(res).get("ok"):
                    return res
        artist = str(args.get("artist") or "").strip()
        entity_id = str(args.get("entity_id") or "").strip()
        # Strip a filler-phrase title ("ein Song von Queen") the model wrongly put
        # in `title`: the trailing name is the artist, the title is empty.
        if title and not artist:
            m = _FILLER_TITLE_RE.match(title)
            if m:
                artist = m.group("artist").strip()
                title = ""
        caller = _caller()
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
        conn = projection.open_conn(db_path)
        try:
            if title:
                # Priority: check if title is actually an artist/band name
                hit_artist = _band_first_castable(conn, check_str, caller)
                if hit_artist is not None:
                    clean, audio_id = hit_artist
                    artist = check_str
                else:
                    song_id = _resolve_song_id(conn, title, artist, caller)
                    if song_id is not None:
                        song = projection.entity_row(conn, song_id)
                        clean = song["canonical_name"]
                        audio_id = _song_audio_id(conn, song_id, caller)
                    elif artist:
                        # An unresolved title with an artist falls back to that artist's
                        # first castable track — never echo the unresolved title.
                        hit = _band_first_castable(conn, artist, caller)
                        if hit is None:
                            return json.dumps(
                                {"ok": False, "reason": "not_found", "query": title},
                                ensure_ascii=False,
                            )
                        clean, audio_id = hit
                    else:
                        return json.dumps(
                            {"ok": False, "reason": "not_found", "query": title},
                            ensure_ascii=False,
                        )
            elif artist:
                hit = _band_first_castable(conn, artist, caller)
                if hit is None:
                    return json.dumps({"ok": False, "reason": "artist_not_found"})
                clean, audio_id = hit
            else:
                hit = _random_castable(conn, caller)
                if hit is None:
                    return json.dumps({"ok": False, "reason": "no_stream"})
                clean, audio_id = hit
        finally:
            conn.close()
        if jellyfin_client is None or not audio_id:
            return json.dumps({"ok": False, "reason": "no_stream"})
        # No castable URL at all (no token) -> honest no_stream, not play_failed.
        if not await jellyfin_client.stream_url(audio_id, static=True):
            return json.dumps({"ok": False, "reason": "no_stream"})
        # Cast static-first, /universal on failure (the group-friendly order);
        # if that 500s (a Cast GROUP can't play a URL, #638) retry once on a
        # single device in the same area (the room's Voice PE preferred).
        result, used = await cast_with_fallback(
            lambda target: _cast(target, audio_id), entity_id, area_fallback
        )
        if not result.get("ok"):
            return json.dumps(
                {
                    "ok": False,
                    "reason": "play_failed",
                    "title": clean,
                    "detail": result.get("error", ""),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "title": clean,
                "artist": artist or "",
                "entity_id": used,
                "played": True,
                "audio_id": audio_id,
            },
            ensure_ascii=False,
        )

    def _last_played(session_id: str) -> tuple[str, str] | None:
        """The (audio_id, title) of the newest ok `play_music` step recorded for
        this session, or None. Records written before #645's recorder extension
        carry no `output`/`audio_id` — skipped, never crashed on."""
        if recorder is None:
            return None
        for r in recorder.list_traces():  # newest first
            if r.get("step_kind") != "tool" or r.get("session_id") != session_id:
                continue
            if r.get("tool_name") != "play_music":
                continue
            out = r.get("output") or ""
            try:
                data = json.loads(out)
            except (ValueError, TypeError):
                continue
            audio_id = data.get("audio_id") if isinstance(data, dict) else None
            if data.get("ok") and audio_id:
                return str(audio_id), str(data.get("title") or "")
        return None

    async def playlist_add(args: dict[str, Any]) -> str:
        if jellyfin_client is None:
            return json.dumps({"ok": False, "reason": "no_stream"})
        caller = _caller()
        artist = str(args.get("artist") or "").strip()
        title = str(args.get("track") or "").strip()
        if title:
            conn = projection.open_conn(db_path)
            try:
                song_id = _resolve_song_id(conn, title, artist, caller)
                if song_id is None:
                    return json.dumps(
                        {
                            "ok": False,
                            "reason": "not_found",
                            "say": "Den Song habe ich nicht in der Bibliothek"
                            " gefunden — wie heißt er genau?",
                        },
                        ensure_ascii=False,
                    )
                song = projection.entity_row(conn, song_id)
                clean = song["canonical_name"]
                audio_id = _song_audio_id(conn, song_id, caller)
            finally:
                conn.close()
            if not audio_id:
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "not_found",
                        "say": "Den Song habe ich nicht in der Bibliothek"
                        " gefunden — wie heißt er genau?",
                    },
                    ensure_ascii=False,
                )
        else:
            session_id = session_getter() if session_getter else ""
            hit = _last_played(session_id)
            if hit is None:
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "no_current_track",
                        "say": "Ich weiß nicht, welcher Song gerade lief —"
                        " welchen soll ich hinzufügen?",
                    },
                    ensure_ascii=False,
                )
            audio_id, clean = hit
        name = str(args.get("playlist") or "").strip() or "Favoriten"
        existing = await jellyfin_client.playlists()
        playlist_id = next(
            (pid for pid, pname in existing if pname.casefold() == name.casefold()),
            None,
        )
        if playlist_id is None:
            playlist_id = await jellyfin_client.create_playlist(name, [audio_id])
        else:
            await jellyfin_client.playlist_add(playlist_id, [audio_id])
        return json.dumps(
            {
                "ok": True,
                "title": clean,
                "playlist": name,
                "say": f"„{clean}“ zur Playlist {name} hinzugefügt.",
            },
            ensure_ascii=False,
        )

    tools = [
        Tool(
            name="music_query",
            description=(
                "Beantwortet Fragen zur eigenen Musikbibliothek (nicht"
                " notes_search). op='songs_by_artist' (artist): welche Songs von X"
                " habe ich. op='list_artists' (optional prefix): welche Künstler"
                " habe ich. op='artist_info' (artist): Genre, Kurzbio, Songanzahl."
                " op='song_lyrics' (title, optional artist): der Liedtext, live aus"
                " der Bibliothek. op='wishlist': welche Alben soll ich mir noch"
                " anschaffen (gewollt/früher geliebt, aber weder digital noch"
                " physisch da). Exakter Treffer gewinnt, sonst unscharf."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": [
                            "songs_by_artist",
                            "list_artists",
                            "artist_info",
                            "song_lyrics",
                            "wishlist",
                        ],
                    },
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                    "prefix": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["op"],
            },
            handler=music_query,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
    # Playlist write needs only a live Jellyfin client (no HA cast); playlists
    # land under the shared service account, matching the shared-library posture.
    if jellyfin_client is not None:
        tools.append(
            Tool(
                name="playlist_add",
                description=(
                    "Fügt einen Song aus der eigenen Bibliothek einer"
                    " Jellyfin-Playlist hinzu ('füge das meiner Playlist hinzu',"
                    " 'pack den Song auf die Playlist'). Ohne track wird der ZULETZT"
                    " GESPIELTE Song genommen — track NUR setzen, wenn der Nutzer"
                    " einen Titel NENNT (wortwörtlich). playlist = Name nur, wenn"
                    " genannt; sonst weglassen. NICHT zum Abspielen (play_music) und"
                    " NICHT für Radiosender. Liefert das Ergebnis 'say', sprich"
                    " diese Zeile wörtlich; melde nur den zurückgegebenen 'title'"
                    " und Playlist-Namen aus der Antwort."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "track": {"type": "string"},
                        "artist": {"type": "string"},
                        "playlist": {"type": "string"},
                    },
                },
                handler=playlist_add,
                visibility=Visibility.HOUSEHOLD,
            )
        )
    # A distinct tool name (not another music_query op) steers far better: the
    # model reaches for play_music on "spiele Musik von X" instead of
    # media_find_podcast (#604). Registered only with a live Jellyfin client +
    # HA creds — without them there's nothing to cast.
    if jellyfin_client is not None and hass_url and hass_token:
        tools.append(
            Tool(
                name="play_music",
                description=(
                    "Spielt Musik aus der EIGENEN Bibliothek (Jellyfin) auf einem"
                    " Raum-Gerät — für 'Spiele Musik/einen Song von <Künstler>',"
                    " 'Spiel <Songtitel>'; NIE media_find_podcast. artist = der"
                    " Künstler; title = NUR der Songtitel, nie Füllwörter (Song/ein/"
                    "von/Musik/Lied). 'Musik/etwas von X' ⇒ artist=X, title LEER."
                    " 'Spiele Musik' ⇒ beide leer (Zufallssong). entity_id"
                    " (media_player des Raums) NUR setzen, wenn der Nutzer"
                    " Gerät/Raum nennt; sonst weglassen. Liefert das Ergebnis"
                    " 'say', sprich diese Zeile wörtlich; nennt die Antwort ein"
                    " Gerät, rufe erneut mit entity_id=<Gerät> auf. Bestätige NUR"
                    " den zurückgegebenen 'title' — erfinde keinen; bei ok:false"
                    " spiele nichts Anderes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "artist": {"type": "string"},
                        "entity_id": {"type": "string"},
                    },
                },
                handler=play_music,
                visibility=Visibility.HOUSEHOLD,
            )
        )
    return tools
