"""CardDAV PUT write path: the contacts importer PUTs each card to a Radicale
address-book collection over HTTP, with the aiohttp session mocked (no network)."""

from __future__ import annotations

from solaris_chat.engine.importers import google_takeout as gt
from solaris_chat.engine.importers.google_takeout.importers import contacts as con
from solaris_chat.engine.ingest import dav_client
from solaris_chat.engine.ingest.dav_client import HttpDavClient

VCF = b"""BEGIN:VCARD
VERSION:3.0
FN:Ada Lovelace
N:Lovelace;Ada;;;
UID:ada/slash@g
TEL:+491234
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Grace Hopper
N:Hopper;Grace;;;
END:VCARD
"""


class _FakeResp:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass


class _FakeSession:
    """Records each PUT (url, data, headers); no real network."""

    puts: list[tuple[str, bytes, dict]] = []

    def __init__(self, *a, **k):
        self.auth = k.get("auth")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def put(self, url, data=None, headers=None):
        _FakeSession.puts.append((url, data, headers or {}))
        return _FakeResp()


def _patch_session(monkeypatch):
    _FakeSession.puts = []
    monkeypatch.setattr(dav_client.aiohttp, "ClientSession", _FakeSession)


async def test_put_item_uses_uid_href_and_carddav_content_type(monkeypatch):
    _patch_session(monkeypatch)
    client = HttpDavClient(carddav_username="u", carddav_password="p")
    url = await client.put_item(
        "https://dav/u/contacts",
        "ada/slash@g",
        "BEGIN:VCARD\r\n",
        suffix=".vcf",
        content_type="text/vcard; charset=utf-8",
    )
    assert url == "https://dav/u/contacts/ada-slash-g.vcf"  # unsafe chars sanitized
    (put_url, data, headers) = _FakeSession.puts[0]
    assert put_url == "https://dav/u/contacts/ada-slash-g.vcf"
    assert data == b"BEGIN:VCARD\r\n"
    assert headers["Content-Type"].startswith("text/vcard")


async def test_import_to_dav_puts_one_card_per_vcard(monkeypatch):
    _patch_session(monkeypatch)
    client = HttpDavClient(carddav_username="u", carddav_password="p")
    rep = await con.import_to_dav(
        client, "https://dav/u/contacts/", "contacts.vcf", VCF
    )
    assert rep["written"] == 2
    assert rep["target"] == "https://dav/u/contacts/"
    assert len(_FakeSession.puts) == 2
    # The card with no source UID gets a synthetic one and still writes a .vcf.
    assert all(url.endswith(".vcf") for (url, _d, _h) in _FakeSession.puts)


async def test_bad_card_is_skipped_and_the_rest_are_put(monkeypatch):
    """#1189: the DAV write path must isolate a per-card failure too."""
    _patch_session(monkeypatch)
    vcf = (
        b"BEGIN:VCARD\nVERSION:3.0\nNOTE:kein Name, keine Nummer\nEND:VCARD\n"
        b"BEGIN:VCARD\nVERSION:3.0\nFN:Grace Hopper\nN:Hopper;Grace;;;\nEND:VCARD\n"
    )
    client = HttpDavClient(carddav_username="u", carddav_password="p")

    rep = await con.import_to_dav(client, "https://dav/u/contacts/", "c.vcf", vcf)

    assert (rep["written"], rep["skipped"]) == (1, 1)
    assert len(_FakeSession.puts) == 1


async def test_contacts_kind_registered_and_run_writes_via_dav(monkeypatch):
    _patch_session(monkeypatch)
    imp = gt.get("contacts")
    assert imp is not None
    assert isinstance(imp, gt.Importer)  # satisfies the runtime-checkable protocol
    plan = imp.plan({"filename": "contacts.vcf", "vcf_bytes": VCF}, None)
    assert plan.kind == "contacts"
    assert plan.summary["cards"] == 2
    client = HttpDavClient(carddav_username="u", carddav_password="p")
    reports = await imp.run(
        plan, {"client": client, "collection_url": "https://dav/u/contacts/"}
    )
    assert reports[0]["written"] == 2
    assert len(_FakeSession.puts) == 2
