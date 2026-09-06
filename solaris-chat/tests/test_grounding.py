"""Numbers and dates are rendered from records, never generated (#1129, G-1).

Stage 1: `notes_read` hands a life-document back as a structured record plus the
readout rendered from its fields. Stage 2: an answer whose numbers or dates are
not in the retrieval context is discarded and the record is read out verbatim —
the failure mode that matters is a 4B model quietly restating an insurance sum or
a cancellation deadline wrong.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from solaris_chat.engine import date_parse, grounding
from solaris_chat.engine.llama_server import ChatResult
from solaris_chat.engine.tools.notes import build_notes_tools

from tests.test_engine import _SCHEMA, _client

_DOC = """---
type: document
title: ERGO Rechtsschutz
category: insurance
provider: ERGO
policy_number: RS-4711
premium_per_year: 289,90 €
contract_sum: 300.000 €
cancellation_deadline: 2026-09-30
cancellation_notice_period: 3 Monate
---
"""


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def soul(tmp_path) -> str:
    path = tmp_path / "SOUL.md"
    path.write_text("Du bist Solaris.", encoding="utf-8")
    return str(path)


def _context(*outputs: str) -> grounding.Grounding:
    messages = [{"role": "user", "content": "Was zahle ich für die Rechtsschutz?"}]
    messages += [{"role": "tool", "content": o} for o in outputs]
    ctx = grounding.context_from_turn(messages)
    assert ctx is not None
    return ctx


def _record_output() -> str:
    record, _ = grounding.parse_document(_DOC)
    return json.dumps(
        {
            "path": "okf/documents/ergo.md",
            "record": record,
            "verbatim": grounding.render(record),
        },
        ensure_ascii=False,
    )


# -- stage 1: retrieval hands back a record, not markdown -------------------


@pytest.mark.asyncio
async def test_notes_read_returns_a_record_and_its_readout(tmp_path):
    vault = tmp_path / "notes"
    (vault / "okf" / "documents").mkdir(parents=True)
    (vault / "okf" / "documents" / "ergo.md").write_text(_DOC, encoding="utf-8")
    read = next(
        t
        for t in build_notes_tools(str(vault), lambda: "household")
        if t.name == "notes_read"
    )

    payload = json.loads(await read.handler({"path": "okf/documents/ergo.md"}))
    assert payload["record"]["contract_sum"] == "300.000 €"
    assert payload["record"]["cancellation_deadline"] == "2026-09-30"
    # frontmatter-only document: no raw markdown for the model to re-narrate
    assert "content" not in payload
    assert "Versicherungssumme: 300.000 €" in payload["verbatim"]
    # the ISO field reads out German, and the schema's format hints are gone
    assert "Kündigungsfrist-Datum: 30.09.2026" in payload["verbatim"]
    assert "YYYY-MM-DD" not in payload["verbatim"]


@pytest.mark.asyncio
async def test_a_prose_note_still_reads_as_prose(tmp_path):
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "gemuese.md").write_text("Tomaten am 3. Mai gesät.", encoding="utf-8")
    read = next(
        t
        for t in build_notes_tools(str(vault), lambda: "household")
        if t.name == "notes_read"
    )

    payload = json.loads(await read.handler({"path": "gemuese.md"}))
    assert payload["content"].startswith("Tomaten")
    assert "record" not in payload


# -- the deterministic scanners ---------------------------------------------


def test_one_date_in_three_spellings_is_one_day():
    for spelling in ("2026-09-30", "30.09.2026", "30. September 2026"):
        assert [d for _, _, d in date_parse.find_dates(spelling)] == [date(2026, 9, 30)]


def test_german_and_plain_amounts_compare_equal():
    ctx = _context(_record_output())
    # 300.000 € restated as "300000 Euro", 289,90 as "289.90" — same values.
    assert grounding.ungrounded("300000 Euro, 289.90 im Jahr", ctx) == []


# -- stage 2: the checker ----------------------------------------------------


def test_an_answer_built_from_the_record_survives():
    ctx = _context(_record_output())
    answer = "Der Rechtsschutz kostet 289,90 € im Jahr, kündbar bis zum 30.09.2026."
    assert grounding.ground(answer, ctx) == answer


def test_a_wrong_amount_falls_back_to_the_verbatim_record():
    ctx = _context(_record_output())
    # The record says 289,90 — the model says 298,90.
    out = grounding.ground("Der Rechtsschutz kostet 298,90 € im Jahr.", ctx)
    assert "298" not in out
    assert "Beitrag pro Jahr: 289,90 €" in out
    assert out.startswith("Ich lese dir das")


def test_a_wrong_date_falls_back_to_the_verbatim_record():
    ctx = _context(_record_output())
    out = grounding.ground("Du kannst noch bis zum 30.10.2026 kündigen.", ctx)
    assert "30.10.2026" not in out
    assert "Kündigungsfrist-Datum: 30.09.2026" in out


def test_a_derived_number_is_a_generated_number():
    ctx = _context(_record_output())
    # 289,90 / 12 is arithmetic the model did itself — not in the record.
    out = grounding.ground("Das sind etwa 24,16 € im Monat.", ctx)
    assert "24,16" not in out


def test_checker_stays_disarmed_without_a_retrieved_record():
    messages = [
        {"role": "user", "content": "Stell einen Timer auf 7 Minuten."},
        {"role": "tool", "content": json.dumps({"ok": True, "minutes": 7})},
    ]
    assert grounding.context_from_turn(messages) is None


def test_only_this_turns_tool_results_ground_the_answer():
    messages = [
        {"role": "user", "content": "Und die Bausparsumme?"},
        {"role": "tool", "content": json.dumps({"ok": True, "balance": "9999"})},
        {"role": "user", "content": "Was kostet der Rechtsschutz?"},
        {"role": "tool", "content": _record_output()},
    ]
    ctx = grounding.context_from_turn(messages)
    assert ctx is not None
    assert grounding.ungrounded("9999 Euro.", ctx) != []


# -- the loop actually applies it -------------------------------------------


@pytest.mark.asyncio
async def test_loop_discards_a_fabricated_sum(db, soul, tmp_path):
    vault = Path(tmp_path) / "notes"
    (vault / "okf" / "documents").mkdir(parents=True)
    (vault / "okf" / "documents" / "ergo.md").write_text(_DOC, encoding="utf-8")
    from solaris_chat.engine.client import current_uid

    client, _ = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "notes_read",
                            "arguments": {"path": "okf/documents/ergo.md"},
                        }
                    }
                ]
            ),
            ChatResult(content="Die Versicherungssumme beträgt 30.000 €."),
        ],
        tools=build_notes_tools(str(vault), current_uid.get),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Wie hoch ist die Summe?")]

    answer = events[-1]["data"]["messages"][0]["content"]
    assert "30.000" not in answer
    assert "Versicherungssumme: 300.000 €" in answer
    # the discarded sum never reached the resident: a grounded turn streams no
    # deltas, the answer arrives once at turn end.
    streamed = "".join(
        e["data"]["delta"] for e in events if e["type"] == "assistant.delta"
    )
    assert "30.000" not in streamed
