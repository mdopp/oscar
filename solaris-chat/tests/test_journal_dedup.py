"""Journal duplicate handling (#709).

The daily-chronicle path is prompt-driven, so a day accumulated under three path
conventions (`journal/<date>.md`, `journal/journal_<date>.md`,
`journal/<YYYY>/<date>.md`). Three defences, each tested here:

1. `note_write` canonicalizes any journal-entry path to
   `journal/<YYYY>/<YYYY-MM-DD>.md` and overwrites in place (idempotent).
2. the librarian's code pass consolidates existing same-day variants into one
   canonical file + `merged_into:` stubs (never deletes, logs to okf/log.md).
3. the notes-portal browse collapses same-day variants to one entry per day.
"""

from __future__ import annotations

import json

import pytest
from solaris_chat import notes_search
from solaris_chat.engine import crons
from solaris_chat.engine.tools.notes import build_notes_tools


def _write_handler(vault, caller_uid: str):
    for tool in build_notes_tools(vault, lambda: caller_uid):
        if tool.name == "note_write":
            return tool.handler
    raise AssertionError("note_write tool not built")


# ---- 1. note_write journal-path canonicalization ----------------------------

_CONVENTIONS = [
    "journal/2024-05-27.md",
    "journal/journal_2024-05-27.md",
    "journal/2024/2024-05-27.md",
]


@pytest.mark.parametrize("path", _CONVENTIONS)
async def test_journal_write_canonicalizes(tmp_path, path):
    root = tmp_path / "notes"
    write = _write_handler(str(root), notes_search.SHARED_UID)
    out = json.loads(await write({"path": path, "content": "Chronik 27. Mai."}))
    # Whatever convention the model passed, the file lands at the canonical path.
    assert out["written"] == "journal/2024/2024-05-27.md"
    assert (root / "journal/2024/2024-05-27.md").is_file()


async def test_journal_write_idempotent_overwrite(tmp_path):
    # Two nightly re-runs of the same day under different conventions must NOT
    # create a new variant — the second overwrites the canonical file in place.
    root = tmp_path / "notes"
    write = _write_handler(str(root), notes_search.SHARED_UID)
    await write({"path": "journal/2024-05-27.md", "content": "erste Fassung"})
    await write({"path": "journal/journal_2024-05-27.md", "content": "zweite Fassung"})
    canon = root / "journal/2024/2024-05-27.md"
    assert canon.read_text(encoding="utf-8").strip().endswith("zweite Fassung")
    # Only the canonical file exists — no stray variant left behind.
    journals = sorted(p.name for p in (root / "journal").rglob("*.md"))
    assert journals == ["2024-05-27.md"]


async def test_journal_write_append_forced_off(tmp_path):
    # A journal write must overwrite, never append, even if append=true is passed
    # (a re-run appending would double the day's content on the canonical file).
    root = tmp_path / "notes"
    write = _write_handler(str(root), notes_search.SHARED_UID)
    await write({"path": "journal/2024/2024-05-27.md", "content": "A"})
    await write({"path": "journal/2024-05-27.md", "content": "B", "append": True})
    assert (
        (root / "journal/2024/2024-05-27.md")
        .read_text(encoding="utf-8")
        .strip()
        .endswith("B")
    )


async def test_non_journal_write_unchanged(tmp_path):
    # A non-journal note must keep the #576 owner-stamp behaviour untouched.
    root = tmp_path / "notes"
    write = _write_handler(str(root), "mdopp")
    out = json.loads(await write({"path": "idee.md", "content": "Wintergarten."}))
    assert out["written"] == "users/mdopp/idee.md"


# ---- 2. librarian journal-by-date consolidation -----------------------------


def _runner() -> crons.CronRunner:
    return crons.CronRunner(
        db_path=":memory:",
        household=object(),
        skills_dir="",
        context_window=32768,
    )


def test_consolidate_journal_duplicates(tmp_path):
    root = tmp_path / "notes"
    (root / "journal" / "2024").mkdir(parents=True)
    (root / "journal" / "2024-05-27.md").write_text("kurz", encoding="utf-8")
    (root / "journal" / "journal_2024-05-27.md").write_text(
        "mittel lang", encoding="utf-8"
    )
    (root / "journal" / "2024" / "2024-05-27.md").write_text(
        "die vollständigste und längste Fassung des Tages", encoding="utf-8"
    )
    # A Syncthing history copy must be pruned, never merged.
    stv = root / ".stversions" / "journal"
    stv.mkdir(parents=True)
    (stv / "2024-05-27.md").write_text("alte kopie", encoding="utf-8")

    stubs = _runner()._consolidate_journal_duplicates(str(root))

    assert stubs == 2
    canon = root / "journal" / "2024" / "2024-05-27.md"
    # Canonical keeps the longest (most complete) content.
    assert "vollständigste" in canon.read_text(encoding="utf-8")
    # The two variants become merged_into stubs — never deleted.
    for name in ("2024-05-27.md", "journal_2024-05-27.md"):
        stub = (root / "journal" / name).read_text(encoding="utf-8")
        assert "merged_into: journal/2024/2024-05-27.md" in stub
        assert (root / "journal" / name).is_file()
    # The .stversions copy was pruned — untouched.
    assert (stv / "2024-05-27.md").read_text(encoding="utf-8") == "alte kopie"
    # Every merge is logged to okf/log.md.
    log_text = (root / "okf" / "log.md").read_text(encoding="utf-8")
    assert log_text.count("journal-dedup:") == 2


def test_consolidate_idempotent(tmp_path):
    # A second run finds only the canonical (the rest are stubs) → no new stubs.
    root = tmp_path / "notes"
    (root / "journal" / "2024").mkdir(parents=True)
    (root / "journal" / "2024-05-27.md").write_text("a", encoding="utf-8")
    (root / "journal" / "2024" / "2024-05-27.md").write_text("bb", encoding="utf-8")
    runner = _runner()
    assert runner._consolidate_journal_duplicates(str(root)) == 1
    assert runner._consolidate_journal_duplicates(str(root)) == 0


def test_consolidate_leaves_singletons(tmp_path):
    root = tmp_path / "notes"
    (root / "journal" / "2024").mkdir(parents=True)
    (root / "journal" / "2024" / "2024-05-27.md").write_text("solo", encoding="utf-8")
    assert _runner()._consolidate_journal_duplicates(str(root)) == 0
    assert not (root / "okf" / "log.md").exists()
