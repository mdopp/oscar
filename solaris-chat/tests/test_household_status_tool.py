"""The household profile answers "läuft alles?" from a real probe (#1310).

Before this, the household toolbox had no health tool of any kind, so a status
question had nothing to call. Measured on the box: 3 of 3 runs answered it by
calling `ha_get_state` on invented sensor entities, one of them 32 times in
parallel — while `/notes wohnzimmer` hit `notes_search` 3 of 3 with the same
prompt and the same tool list. The difference was the presence of a fitting
tool, so this adds exactly one.

The other half of these tests is what the tool must NOT be. `status/SKILL.md`
used to instruct ServiceBay's `get_health_checks`/`diagnose`, which live only on
the admin profile; the fix must not turn that instruction into a real hole. So
the shape is pinned here: no arguments the model can steer, no ServiceBay
import, no admin tool name on the household toolbox, nothing on guest, and no
internal path or address in the answer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from solaris_chat.engine import profiles
from solaris_chat.engine.tools.status import build_status_tools

# Everything the admin toolbox may reach that a resident's — or a voice guest's
# — turn must not. Naming them explicitly is the point: a future edit that hands
# the household profile the SB-MCP toolbox fails here.
ADMIN_ONLY = (
    "get_health_checks",
    "diagnose",
    "get_logs",
    "get_service_files",
    "list_containers",
    "list_services",
    "manage_service",
    "deploy_service",
    "install_template",
    "update_service_yaml",
    "container_exec",
    "exec_command",
    "reboot_node",
    "run_check_now",
    "create_health_check",
)


def _db(tmp_path: Path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def _tool(**kwargs):
    tools = build_status_tools(**kwargs)
    assert len(tools) == 1
    return tools[0]


# -- it exists, and it is the household's ------------------------------------


def _profiles(tmp_path: Path):
    household, admin, guest, _lib, _enroll, _rec, _bus = profiles.build_engine_clients(
        db_path=_db(tmp_path),
        llama_server_url="http://ollama",
        fast_model="m",
        thorough_model="m",
        soul_path="",
        hass_url="http://ha",
        hass_token="tok",
        gatekeeper_url="http://gk",
        sb_mcp_url="http://sb/mcp",
        sb_mcp_token_path="/tmp/token",
    )
    return household, admin, guest


def test_the_household_profile_can_answer_a_status_question(tmp_path):
    household, _admin, _guest = _profiles(tmp_path)
    assert "get_solaris_status" in household._profile.toolbox.names()


def test_a_guest_turn_does_not_get_it(tmp_path):
    _household, _admin, guest = _profiles(tmp_path)
    assert "get_solaris_status" not in guest._profile.toolbox.names()


def test_the_household_toolbox_holds_no_admin_tool(tmp_path):
    """The reason the probe exists in code rather than as a household grant of
    the SB-MCP toolbox: none of the operator tools may become reachable."""
    household, _admin, _guest = _profiles(tmp_path)
    names = set(household._profile.toolbox.names())
    assert not names & set(ADMIN_ONLY)


def test_the_probe_reaches_no_servicebay_code():
    source = (Path(profiles.__file__).parent / "tools" / "status.py").read_text(
        encoding="utf-8"
    )
    assert "mcp_tools" not in source
    assert "McpToolbox" not in source


# -- it cannot be steered ----------------------------------------------------


def test_it_takes_no_arguments_at_all(tmp_path):
    tool = _tool(db_path=_db(tmp_path))
    assert tool.parameters == {"type": "object", "properties": {}}


async def test_arguments_the_model_invents_change_nothing(tmp_path):
    tool = _tool(db_path=_db(tmp_path))
    plain = json.loads(await tool.handler({}))
    steered = json.loads(
        await tool.handler({"service": "gatekeeper", "action": "restart"})
    )
    assert plain == steered


# -- it reports what is true -------------------------------------------------


async def test_a_readable_store_reports_ok(tmp_path):
    result = json.loads(await _tool(db_path=_db(tmp_path)).handler({}))
    assert result == {"alles_ok": True, "teile": [{"teil": "Gedächtnis", "ok": True}]}


async def test_an_unreadable_store_reports_not_ok(tmp_path):
    # A directory where solaris.db should be — unopenable whatever uid we run as.
    result = json.loads(await _tool(db_path=str(tmp_path)).handler({}))
    assert result["alles_ok"] is False
    assert result["teile"] == [{"teil": "Gedächtnis", "ok": False}]


async def test_an_unreachable_part_is_reported_not_invented(tmp_path):
    """The failure this replaces: the model guessing. An unreachable Home
    Assistant must come back as one false boolean, not as an exception the model
    then narrates around."""
    tool = _tool(
        db_path=_db(tmp_path),
        # Reserved TEST-NET-1 (RFC 5737) on a closed port: never routable.
        hass_url="http://192.0.2.1:1",
        hass_token="tok",
    )
    result = json.loads(await tool.handler({}))
    assert result["alles_ok"] is False
    assert {p["teil"]: p["ok"] for p in result["teile"]} == {
        "Gedächtnis": True,
        "Haussteuerung": False,
    }


# -- and it says nothing a resident should not hear --------------------------


@pytest.mark.parametrize("db_ok", [True, False])
async def test_the_answer_leaks_no_path_address_or_token(tmp_path, db_ok):
    db_path = _db(tmp_path) if db_ok else str(tmp_path)
    tool = _tool(
        db_path=db_path,
        hass_url="http://192.0.2.1:1",
        hass_token="super-secret-token",
        gatekeeper_url="http://192.0.2.1:2",
    )
    answer = await tool.handler({})
    for secret in (db_path, "192.0.2.1", "super-secret-token", "http"):
        assert secret not in answer
    # Only the three plain-language part names ever appear.
    assert {p["teil"] for p in json.loads(answer)["teile"]} == {
        "Gedächtnis",
        "Haussteuerung",
        "Sprachsteuerung",
    }


async def test_an_unconfigured_part_is_not_claimed(tmp_path):
    """An install without HA or without the voice bridge reports on what it
    has, so `alles_ok` can never go false for a part that was never installed —
    and the tool disappears entirely when there is nothing to report."""
    assert build_status_tools() == []
    result = json.loads(await _tool(db_path=_db(tmp_path)).handler({}))
    assert [p["teil"] for p in result["teile"]] == ["Gedächtnis"]
