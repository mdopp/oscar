import json

from solaris_chat.engine.importers.google_takeout.importers import contacts as con

VCF = b"""BEGIN:VCARD
VERSION:3.0
FN:Max Mustermann
EMAIL:max@example.com
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Erika Mueller
UID:erika-123
END:VCARD
"""


def _user_root(paths, user):
    return paths.radicale_data / "collections" / "collection-root" / user


def test_preview():
    p = con.preview("c.vcf", VCF)
    assert p["cards"] == 2
    assert "Max Mustermann" in p["samples"]


def test_import_writes_two_cards_and_props(paths):
    rep = con.do_import(paths.radicale_data, "conu1", "c.vcf", VCF)
    assert rep["written"] == 2
    cdir = _user_root(paths, "conu1") / "contacts"
    assert len(list(cdir.glob("*.vcf"))) == 2
    assert json.loads((cdir / ".Radicale.props").read_text())["tag"] == "VADDRESSBOOK"


def test_generated_uid_for_card_without_one(paths):
    con.do_import(paths.radicale_data, "conu2", "c.vcf", VCF)
    cdir = _user_root(paths, "conu2") / "contacts"
    # the card with an explicit UID keeps it
    assert (cdir / "erika-123.vcf").exists()


def _card(*lines: str) -> str:
    return (
        "BEGIN:VCARD\nVERSION:3.0\n"
        + "".join(f"{ln}\n" for ln in lines)
        + "END:VCARD\n"
    )


def test_nameless_card_does_not_abort_the_import(paths):
    """#1189: a `FN`-less card used to raise and zero the whole category."""
    vcf = (
        _card("TEL;TYPE=CELL:+49123456") + _card("FN:Grace Hopper", "N:Hopper;Grace;;;")
    ).encode()

    rep = con.do_import(paths.radicale_data, "conu3", "c.vcf", vcf)

    assert (rep["written"], rep["skipped"]) == (2, 0)
    cdir = _user_root(paths, "conu3") / "contacts"
    assert len(list(cdir.glob("*.vcf"))) == 2
    # the nameless card is labelled with its own number rather than dropped
    assert any("+49123456" in p.read_text() for p in cdir.glob("*.vcf"))


def test_unwritable_card_is_counted_and_the_rest_still_land(paths):
    """#1189: an unusable card costs itself only, and the resident is told."""
    vcf = (
        _card("NOTE:kein Name, keine Nummer")
        + _card("FN:Grace Hopper", "N:Hopper;Grace;;;")
    ).encode()

    rep = con.do_import(paths.radicale_data, "conu4", "c.vcf", vcf)

    assert (rep["written"], rep["skipped"]) == (1, 1)
    assert len(list((_user_root(paths, "conu4") / "contacts").glob("*.vcf"))) == 1


def test_uidless_card_overwrites_itself_on_a_changed_reimport(paths):
    """#1190: a re-export with an added phone number must not duplicate."""
    first = _card(
        "FN:Grace Hopper", "N:Hopper;Grace;;;", "EMAIL:grace@example.com"
    ).encode()
    second = _card(
        "FN:Grace Hopper",
        "N:Hopper;Grace;;;",
        "EMAIL:grace@example.com",
        "TEL;TYPE=CELL:+49999",
    ).encode()

    con.do_import(paths.radicale_data, "conu5", "c.vcf", first)
    con.do_import(paths.radicale_data, "conu5", "c.vcf", second)

    cards = list((_user_root(paths, "conu5") / "contacts").glob("*.vcf"))
    assert len(cards) == 1
    assert "+49999" in cards[0].read_text()


def test_two_people_sharing_a_name_stay_two_cards(paths):
    """The synthetic key is name + strongest identifier, so namesakes survive."""
    vcf = (
        _card("FN:Max Mustermann", "EMAIL:max1@example.com")
        + _card("FN:Max Mustermann", "EMAIL:max2@example.com")
    ).encode()

    con.do_import(paths.radicale_data, "conu6", "c.vcf", vcf)

    assert len(list((_user_root(paths, "conu6") / "contacts").glob("*.vcf"))) == 2
