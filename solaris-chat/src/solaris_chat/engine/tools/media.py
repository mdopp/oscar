"""Podcast tools — find a show on fyyd.de and play its newest episode.

fyyd.de is a free, keyless, open podcast index (no key, no account, no id). The
search endpoint resolves a show by name; its `xmlURL` is the RSS feed whose
newest `<enclosure>` is the episode audio. Played through the same scoped
`media_player.play_media` path the card actions use (#476/#511/#512) so the
engine never holds a client-side HA token.

Defensive by design: a fyyd/feed shape mismatch or an unreachable index fails
gracefully ("not found") rather than crashing — the real-box /verify (with
internet) confirms live reachability.
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import aiohttp

from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.ha import call_service_scoped
from solaris_chat.engine.tools.radio import cast_with_fallback

_FYYD_SEARCH = "https://api.fyyd.de/0.2/search/podcast"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


_GOOD_MATCH = 0.6


def _best_by_title(
    name: str, candidates: list[dict[str, Any]]
) -> tuple[float, dict[str, Any]] | None:
    """Score candidates by fuzzy title similarity; return (score, show) of the best."""
    target = name.casefold().strip()
    best: tuple[float, dict[str, Any]] | None = None
    for show in candidates:
        title = str(show.get("title") or "").casefold().strip()
        if not title:
            continue
        score = SequenceMatcher(None, target, title).ratio()
        if best is None or score > best[0]:
            best = (score, show)
    return best


def _pick_best(name: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the candidate whose title is closest to `name`, else the top hit.

    fyyd ranks by relevance, but a slightly-off query ("Netzpolitik" vs the
    show's exact title) can rank a wrong show first. Score each by fuzzy title
    similarity (casefold/strip-normalized) and prefer the closest; fall back to
    fyyd's top result when nothing is meaningfully close.
    """
    best = _best_by_title(name, candidates)
    if best is not None and best[0] >= _GOOD_MATCH:
        return best[1]
    return candidates[0] if candidates else None


def _feed_of(show: dict[str, Any], name: str) -> dict[str, Any] | None:
    feed = str(show.get("xmlURL") or "").strip()
    if not feed.startswith("http"):
        return None
    return {"title": str(show.get("title") or name), "feed": feed}


async def _fyyd_query(
    client: aiohttp.ClientSession, param: str, name: str
) -> list[dict[str, Any]]:
    """One keyless fyyd search; `param` is 'title' (show titles) or 'term' (title+host+desc)."""
    async with client.get(_FYYD_SEARCH, params={param: name, "count": 5}) as resp:
        if resp.status >= 400:
            return []
        body = await resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict)]


async def _fyyd_resolve_feed(name: str) -> dict[str, Any] | None:
    """Resolve a show name to its {title, feed} via fyyd's keyless search.

    The `title=` search matches show titles only, so "Tim Pritlove" (a host, not
    a title) finds nothing there. When the title search yields no meaningfully
    close match, fall back to fyyd's `term=` search, which also matches the
    author/host and description — resolving "Podcasts von <Person>" to one of
    that person's shows (#568).
    """
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
        title_hits = await _fyyd_query(client, "title", name)
        best = _best_by_title(name, title_hits)
        if best is not None and best[0] >= _GOOD_MATCH:
            return _feed_of(best[1], name)

        term_hits = await _fyyd_query(client, "term", name)
        show = _pick_best(name, term_hits) or (title_hits[0] if title_hits else None)
    if show is None:
        return None
    return _feed_of(show, name)


def _newest_enclosure(feed_xml: str) -> dict[str, str] | None:
    """Newest item's enclosure URL + title from an RSS feed.

    Items are usually newest-first, but pick by `pubDate` so a misordered feed
    still yields the latest episode.
    """
    root = ElementTree.fromstring(feed_xml)
    items = root.findall(".//item")
    best: tuple[float, dict[str, str]] | None = None
    for idx, item in enumerate(items):
        enclosure = item.find("enclosure")
        url = enclosure.get("url") if enclosure is not None else None
        if not url:
            continue
        title_el = item.find("title")
        ep = {
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "url": url.strip(),
        }
        when = item.find("pubDate")
        ts = -float(idx)  # document order fallback (earlier = newer)
        if when is not None and when.text:
            try:
                ts = parsedate_to_datetime(when.text).timestamp()
            except (TypeError, ValueError):
                pass
        if best is None or ts > best[0]:
            best = (ts, ep)
    return best[1] if best else None


def build_media_tools(
    hass_url: str, hass_token: str, *, area_fallback=None
) -> list[Tool]:
    async def find_podcast(args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return json.dumps({"ok": False, "reason": "missing_name"})
        entity_id = str(args.get("entity_id") or "").strip()

        show = await _fyyd_resolve_feed(name)
        if show is None:
            return json.dumps(
                {"ok": False, "reason": "show_not_found", "query": name},
                ensure_ascii=False,
            )
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(show["feed"]) as resp:
                if resp.status >= 400:
                    return json.dumps(
                        {
                            "ok": False,
                            "reason": "feed_unavailable",
                            "show": show["title"],
                        },
                        ensure_ascii=False,
                    )
                feed_xml = await resp.text()
        try:
            episode = _newest_enclosure(feed_xml)
        except ElementTree.ParseError:
            episode = None
        if episode is None:
            return json.dumps(
                {"ok": False, "reason": "no_episode", "show": show["title"]},
                ensure_ascii=False,
            )

        if not entity_id:
            # No device given — hand the resolved episode back so the model can
            # ask which room, then call again with entity_id.
            return json.dumps(
                {
                    "ok": True,
                    "show": show["title"],
                    "episode": episode["title"],
                    "media_url": episode["url"],
                    "played": False,
                },
                ensure_ascii=False,
            )

        async def _cast(target: str) -> dict[str, Any]:
            return await call_service_scoped(
                hass_url,
                hass_token,
                target,
                "media_player.play_media",
                {"media_content_type": "music", "media_content_id": episode["url"]},
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
                    "show": show["title"],
                    "episode": episode["title"],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "show": show["title"],
                "episode": episode["title"],
                "media_url": episode["url"],
                "entity_id": used,
                "played": True,
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="media_find_podcast",
            description=(
                "Findet einen Podcast über den Index fyyd.de und spielt die"
                " neueste Folge auf einem Raum-Gerät — für 'Spiel die neueste Folge"
                " von <Podcast>' und 'Podcasts von <Person>'. name = GENAU der"
                " gesagte Podcast-/Personenname, WORTWÖRTLICH — niemals korrigieren,"
                " übersetzen oder ersetzen ('tim pritlove' bleibt 'tim pritlove');"
                " der Index fuzzy-matcht selbst. entity_id = media_player des"
                " Zielraums; ohne entity_id wird nur aufgelöst — frag nach dem Raum"
                " und rufe erneut auf. NUR Podcasts — Musik: play_music, Radio:"
                " play_radio."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "entity_id": {"type": "string"},
                },
                "required": ["name"],
            },
            handler=find_podcast,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
