"""Podcast tool tests (#513).

aiohttp is stubbed so the fyyd search, the RSS feed fetch, and the HA
play_media POST are exercised with no live network; the handler's request
shapes + graceful-failure paths are asserted.
"""

from __future__ import annotations

import json

from solaris_chat.engine.tools import media as media_mod
from solaris_chat.engine.tools.media import (
    _fyyd_resolve_feed,
    _newest_enclosure,
    build_media_tools,
)


class _Resp:
    def __init__(self, *, json_body=None, text_body="", status=200):
        self._json = json_body
        self._text = text_body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


_FEED = """<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Folge 100</title>
    <pubDate>Wed, 18 Jun 2026 06:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example/100.mp3" type="audio/mpeg"/>
  </item>
  <item>
    <title>Folge 99</title>
    <pubDate>Wed, 11 Jun 2026 06:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example/99.mp3" type="audio/mpeg"/>
  </item>
</channel></rss>"""


def _stub(monkeypatch, *, search=None, feed=_FEED, posts=None, feed_status=200):
    """Stub aiohttp: first GET = fyyd search, second GET = feed; record POSTs."""
    gets: list[tuple[str, dict]] = []
    search_body = (
        search
        if search is not None
        else {"data": [{"title": "Lage der Nation", "xmlURL": "https://feed/ldn.xml"}]}
    )

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, geturl, *, params=None, **k):
            gets.append((geturl, params or {}))
            if "api.fyyd.de" in geturl:
                return _Resp(json_body=search_body)
            if "/api/states/" in geturl:
                # call_service_scoped reads back the player state after play
                return _Resp(json_body={"state": "playing"})
            return _Resp(text_body=feed, status=feed_status)

        def post(self, posturl, *, json, **k):
            if posts is not None:
                posts.append((posturl, json))
            return _Resp(json_body={"state": "playing"})

    monkeypatch.setattr(media_mod.aiohttp, "ClientSession", _Session)
    return gets


def _tool():
    return build_media_tools("http://ha", "tok")[0]


def test_newest_enclosure_picks_latest_by_pubdate():
    ep = _newest_enclosure(_FEED)
    assert ep == {"title": "Folge 100", "url": "https://cdn.example/100.mp3"}


def test_newest_enclosure_skips_items_without_enclosure():
    feed = """<rss><channel>
      <item><title>no audio</title></item>
      <item><title>has audio</title>
        <enclosure url="https://cdn/x.mp3"/></item>
    </channel></rss>"""
    assert _newest_enclosure(feed) == {"title": "has audio", "url": "https://cdn/x.mp3"}


async def test_find_and_play_resolves_show_feed_and_plays(monkeypatch):
    posts: list = []
    gets = _stub(monkeypatch, posts=posts)
    out = json.loads(
        await _tool().handler(
            {"name": "Lage der Nation", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is True and out["played"] is True
    assert out["show"] == "Lage der Nation"
    assert out["episode"] == "Folge 100"
    assert out["media_url"] == "https://cdn.example/100.mp3"
    # fyyd search hit with the title param, then the feed url fetched
    assert any(
        "api.fyyd.de" in u and p.get("title") == "Lage der Nation" for u, p in gets
    )
    assert any(u == "https://feed/ldn.xml" for u, _ in gets)
    # play_media POSTed to HA with the newest enclosure as content id
    posturl, body = posts[0]
    assert posturl == "http://ha/api/services/media_player/play_media"
    assert body["media_content_id"] == "https://cdn.example/100.mp3"
    assert body["media_content_type"] == "music"
    assert body["entity_id"] == "media_player.wohnzimmer"


async def test_find_without_entity_resolves_only_no_play(monkeypatch):
    posts: list = []
    _stub(monkeypatch, posts=posts)
    out = json.loads(await _tool().handler({"name": "Lage der Nation"}))
    assert out["ok"] is True and out["played"] is False
    assert out["media_url"] == "https://cdn.example/100.mp3"
    assert posts == []  # no device => no HA call


async def test_resolve_feed_picks_closest_title_over_top_hit(monkeypatch):
    # fyyd ranks a near-miss first; the exact-ish match comes later in the list.
    gets = _stub(
        monkeypatch,
        search={
            "data": [
                {"title": "Netropolitik Daily", "xmlURL": "https://feed/wrong.xml"},
                {"title": "Netzpolitik", "xmlURL": "https://feed/netzpolitik.xml"},
                {"title": "Something Else", "xmlURL": "https://feed/else.xml"},
            ]
        },
    )
    show = await _fyyd_resolve_feed("Netzpolitik")
    assert show == {"title": "Netzpolitik", "feed": "https://feed/netzpolitik.xml"}
    # requested more than one candidate so a best-match is possible
    assert any("api.fyyd.de" in u and p.get("count") == 5 for u, p in gets)


def _stub_split(monkeypatch, *, title_body, term_body, feed=_FEED):
    """Stub aiohttp: fyyd GET returns title_body for title=, term_body for term=."""
    gets: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, geturl, *, params=None, **k):
            params = params or {}
            gets.append((geturl, params))
            if "api.fyyd.de" in geturl:
                if "term" in params:
                    return _Resp(json_body=term_body)
                return _Resp(json_body=title_body)
            return _Resp(text_body=feed)

        def post(self, posturl, *, json, **k):
            return _Resp(json_body={"state": "playing"})

    monkeypatch.setattr(media_mod.aiohttp, "ClientSession", _Session)
    return gets


async def test_resolve_feed_falls_back_to_host_term_when_title_misses(monkeypatch):
    # A host name ("Tim Pritlove") finds no show *title*, so fall back to the
    # term= search (matches author/host) and fuzzy-pick one of his shows (#568).
    gets = _stub_split(
        monkeypatch,
        title_body={"data": []},
        term_body={
            "data": [
                {"title": "Freak Show", "xmlURL": "https://feed/freakshow.xml"},
                {"title": "Logbuch:Netzpolitik", "xmlURL": "https://feed/lnp.xml"},
            ]
        },
    )
    show = await _fyyd_resolve_feed("Tim Pritlove")
    assert show == {"title": "Freak Show", "feed": "https://feed/freakshow.xml"}
    # tried title= first (miss), then fell back to term=
    assert any("api.fyyd.de" in u and p.get("title") == "Tim Pritlove" for u, p in gets)
    assert any("api.fyyd.de" in u and p.get("term") == "Tim Pritlove" for u, p in gets)


async def test_resolve_feed_title_hit_skips_term_search(monkeypatch):
    # A good title match returns straight away — no needless term= fallback.
    gets = _stub_split(
        monkeypatch,
        title_body={
            "data": [{"title": "Netzpolitik", "xmlURL": "https://feed/nz.xml"}]
        },
        term_body={"data": [{"title": "WRONG", "xmlURL": "https://feed/wrong.xml"}]},
    )
    show = await _fyyd_resolve_feed("Netzpolitik")
    assert show == {"title": "Netzpolitik", "feed": "https://feed/nz.xml"}
    assert not any("term" in p for _, p in gets)


async def test_resolve_feed_falls_back_to_top_when_none_close(monkeypatch):
    _stub(
        monkeypatch,
        search={
            "data": [
                {"title": "Totally Unrelated Show", "xmlURL": "https://feed/top.xml"},
                {"title": "Another Random One", "xmlURL": "https://feed/b.xml"},
            ]
        },
    )
    show = await _fyyd_resolve_feed("Tim Pritlove")
    assert show == {"title": "Totally Unrelated Show", "feed": "https://feed/top.xml"}


def test_find_podcast_description_demands_verbatim_name():
    desc = _tool().description
    assert "WORTWÖRTLICH" in desc
    # explicitly forbids rewriting/correcting/translating the name
    assert "niemals korrigieren" in desc
    assert "übersetzen" in desc and "ersetzen" in desc


def test_find_podcast_description_excludes_music(monkeypatch):
    # Narrowed to podcasts-only so a music request steers to play_music (#604).
    desc = _tool().description
    assert "NUR Podcasts" in desc
    assert "play_music" in desc


# -- group-cast fallback (#638/#573): a 500 on a Cast group → same-area device --


def _stub_cast_seq(monkeypatch, results):
    """Stub media's call_service_scoped with a queued result sequence, recording targets."""
    calls: list[tuple] = []
    seq = list(results)

    async def _fake_cast(hass_url, hass_token, entity_id, service, data):
        calls.append((entity_id, data))
        return seq[min(len(calls) - 1, len(seq) - 1)]

    monkeypatch.setattr(media_mod, "call_service_scoped", _fake_cast)
    return calls


def _tool_with_fallback(fallbacks):
    async def _area_fallback(entity_id):
        return list(fallbacks)

    return build_media_tools("http://ha", "tok", area_fallback=_area_fallback)[0]


async def test_podcast_group_500_falls_back_to_same_area_device(monkeypatch):
    # Resolve is stubbed via aiohttp; the play goes through call_service_scoped.
    _stub(monkeypatch)
    calls = _stub_cast_seq(
        monkeypatch,
        [
            {"ok": False, "error": "HA 500: group reject"},
            {"ok": True, "state": "playing"},
        ],
    )
    play = _tool_with_fallback(["media_player.voice_pe"])
    out = json.loads(
        await play.handler(
            {"name": "Lage der Nation", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is True and out["played"] is True
    # Reports the device that ACTUALLY played, not the failed group.
    assert out["entity_id"] == "media_player.voice_pe"
    assert calls[0][0] == "media_player.wohnzimmer"
    assert calls[1][0] == "media_player.voice_pe"


async def test_podcast_group_500_no_candidate_returns_play_failed(monkeypatch):
    _stub(monkeypatch)
    calls = _stub_cast_seq(
        monkeypatch, [{"ok": False, "error": "HA 500: group reject"}]
    )
    play = _tool_with_fallback([])  # no same-area device
    out = json.loads(
        await play.handler(
            {"name": "Lage der Nation", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert out["detail"] == "HA 500: group reject"
    assert len(calls) == 1  # honest failure, no extra cast


async def test_podcast_non_500_failure_no_fallback(monkeypatch):
    _stub(monkeypatch)
    calls = _stub_cast_seq(monkeypatch, [{"ok": False, "error": "HA 404: not found"}])
    play = _tool_with_fallback(["media_player.voice_pe"])
    out = json.loads(
        await play.handler(
            {"name": "Lage der Nation", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert len(calls) == 1  # a 404 must NOT trigger the same-area fallback


async def test_show_not_found_is_graceful(monkeypatch):
    _stub(monkeypatch, search={"data": []})
    out = json.loads(
        await _tool().handler({"name": "nope", "entity_id": "media_player.x"})
    )
    assert out == {"ok": False, "reason": "show_not_found", "query": "nope"}


async def test_missing_name_is_graceful(monkeypatch):
    out = json.loads(await _tool().handler({"entity_id": "media_player.x"}))
    assert out == {"ok": False, "reason": "missing_name"}


async def test_feed_with_no_enclosure_is_graceful(monkeypatch):
    _stub(monkeypatch, feed="<rss><channel></channel></rss>")
    out = json.loads(
        await _tool().handler(
            {"name": "Lage der Nation", "entity_id": "media_player.x"}
        )
    )
    assert out["ok"] is False and out["reason"] == "no_episode"


async def test_unparseable_feed_is_graceful(monkeypatch):
    _stub(monkeypatch, feed="<not xml")
    out = json.loads(
        await _tool().handler(
            {"name": "Lage der Nation", "entity_id": "media_player.x"}
        )
    )
    assert out["ok"] is False and out["reason"] == "no_episode"


async def test_feed_with_xxe_entity_is_rejected_gracefully(monkeypatch):
    """A hostile feed's external entity is refused, not resolved (#1225)."""
    feed = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<rss><channel><item><title>&xxe;</title>"
        '<enclosure url="https://cdn/x.mp3"/></item></channel></rss>'
    )
    _stub(monkeypatch, feed=feed)
    out = json.loads(
        await _tool().handler(
            {"name": "Lage der Nation", "entity_id": "media_player.x"}
        )
    )
    assert out["ok"] is False and out["reason"] == "no_episode"
