"""Tests for the dynamic-skill promotion via the generic SB approval API (#427).

The SB-MCP calls (file_access_request / get_access_request_status) are mocked by
monkeypatching `call_sb_tool`, so we assert the Solaris side only: a pending
draft is filed with the right shape (slug as subject, kind="skill"), the request
id is recorded next to the draft, the status is polled, and the tri-state verdict
is handled — "approved" promotes the draft into the active pack (the engine moves
it itself, no restart); "denied"/"not-found" drops the draft; "pending" promotes
nothing. The filesystem is a tmp skills dir, so nothing real is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from solaris_chat.engine.tools import skill_promotion

_SKILL = "---\nname: solaris-custom-x\n---\n# X\n"


def _draft(skills_dir: Path, slug: str) -> Path:
    pending = skills_dir / "_pending" / slug
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return pending


def _stub_sb(monkeypatch, replies: dict[str, dict]) -> list[tuple]:
    calls: list[tuple] = []

    async def fake(url, token_path, name, arguments):
        calls.append((name, arguments))
        return json.dumps(replies[name])

    monkeypatch.setattr(skill_promotion, "call_sb_tool", fake)
    return calls


def _tools(skills_dir: Path):
    return {
        t.name: t
        for t in skill_promotion.build_skill_promotion_tools(
            str(skills_dir), "http://sb/mcp", "/tmp/token"
        )
    }


async def test_file_approval_files_request_with_right_shape(tmp_path, monkeypatch):
    _draft(tmp_path, "weather")
    calls = _stub_sb(monkeypatch, {"file_access_request": {"id": "req-9"}})

    out = json.loads(
        await _tools(tmp_path)["file_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {"ok": True, "request_id": "req-9", "status": "filed"}

    name, args = calls[0]
    assert name == "file_access_request"
    assert args["subject"] == "weather"
    assert args["kind"] == "skill"
    assert args["requested_by"] == "solaris-skills"

    rid = (tmp_path / "_pending" / "weather" / ".request_id").read_text().strip()
    assert rid == "req-9"


async def test_file_approval_no_pending_skill(tmp_path, monkeypatch):
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(
        await _tools(tmp_path)["file_skill_approval"].handler({"slug": "ghost"})
    )
    assert out == {"ok": False, "reason": "no_pending_skill"}
    assert calls == []


async def test_file_approval_invalid_slug(tmp_path, monkeypatch):
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(
        await _tools(tmp_path)["file_skill_approval"].handler({"slug": "../escape"})
    )
    assert out == {"ok": False, "reason": "invalid_slug"}
    assert calls == []


async def test_file_approval_is_idempotent(tmp_path, monkeypatch):
    pending = _draft(tmp_path, "weather")
    (pending / ".request_id").write_text("req-7", encoding="utf-8")
    calls = _stub_sb(monkeypatch, {"file_access_request": {"id": "should-not-call"}})

    out = json.loads(
        await _tools(tmp_path)["file_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {"ok": True, "request_id": "req-7", "status": "filed"}
    assert calls == []  # already filed → no second SB call


async def test_check_approval_approved_promotes(tmp_path, monkeypatch):
    pending = _draft(tmp_path, "weather")
    (pending / ".request_id").write_text("req-9", encoding="utf-8")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "approved"}})

    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {
        "ok": True,
        "status": "approved",
        "promoted": True,
        "slug": "weather",
    }

    # The draft moved into the active pack; pending is gone.
    assert (tmp_path / "weather" / "SKILL.md").is_file()
    assert not (tmp_path / "_pending" / "weather").exists()
    # The promoted skill carries no leftover request-id sidecar.
    assert not (tmp_path / "weather" / ".request_id").exists()


async def test_check_approval_denied_drops_draft(tmp_path, monkeypatch):
    pending = _draft(tmp_path, "weather")
    (pending / ".request_id").write_text("req-9", encoding="utf-8")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "denied"}})

    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {"ok": True, "status": "denied", "promoted": False, "slug": "weather"}
    assert not (tmp_path / "_pending" / "weather").exists()
    assert not (tmp_path / "weather").exists()


async def test_check_approval_not_found_drops_draft(tmp_path, monkeypatch):
    pending = _draft(tmp_path, "weather")
    (pending / ".request_id").write_text("req-9", encoding="utf-8")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "not-found"}})

    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "weather"})
    )
    assert out["status"] == "not-found"
    assert out["promoted"] is False
    assert not (tmp_path / "_pending" / "weather").exists()


async def test_check_approval_pending_promotes_nothing(tmp_path, monkeypatch):
    pending = _draft(tmp_path, "weather")
    (pending / ".request_id").write_text("req-9", encoding="utf-8")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "pending"}})

    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {"ok": True, "status": "pending", "promoted": False}
    assert (tmp_path / "_pending" / "weather").exists()
    assert not (tmp_path / "weather").exists()


async def test_check_approval_not_filed(tmp_path, monkeypatch):
    _draft(tmp_path, "weather")
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {"ok": False, "reason": "not_filed"}
    assert calls == []


async def test_check_approval_rejects_slug_with_dotdot(tmp_path, monkeypatch):
    # `..` never reaches the SB poll — the slug regex rejects it up front.
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "../escape"})
    )
    assert out == {"ok": False, "reason": "invalid_slug"}
    assert calls == []


async def test_check_approval_rejects_absolute_slug(tmp_path, monkeypatch):
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(
        await _tools(tmp_path)["check_skill_approval"].handler({"slug": "/etc"})
    )
    assert out == {"ok": False, "reason": "invalid_slug"}
    assert calls == []


async def test_check_approval_rejects_symlinked_pending_dir(tmp_path, monkeypatch):
    # A compromised draft step plants `_pending/evil` as a symlink to a dir
    # OUTSIDE the skills sandbox (e.g. the solaris-data volume root). The slug
    # itself is regex-valid, the .request_id resolves through the symlink, and SB
    # returns "approved" — but the resolved-path containment check must refuse to
    # move the symlink target into the active pack.
    outside = tmp_path / "secret_data"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    (outside / "SKILL.md").write_text(_SKILL, encoding="utf-8")

    pending_root = tmp_path / "skills" / "_pending"
    pending_root.mkdir(parents=True)
    evil = pending_root / "evil"
    evil.symlink_to(outside, target_is_directory=True)
    (evil / ".request_id").write_text("req-9", encoding="utf-8")

    skills_dir = tmp_path / "skills"
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "approved"}})

    out = json.loads(
        await _tools(skills_dir)["check_skill_approval"].handler({"slug": "evil"})
    )
    assert out == {"ok": False, "reason": "path_escape"}
    # Nothing was moved out of the sandbox: the symlink target is untouched and
    # the active pack did not gain the planted dir.
    assert (outside / "secret.txt").is_file()
    assert not (skills_dir / "evil").exists()


async def test_check_approval_normal_promote_still_works(tmp_path, monkeypatch):
    # The hardening must not break the legitimate promote path.
    skills_dir = tmp_path / "skills"
    pending = _draft(skills_dir, "weather")
    (pending / ".request_id").write_text("req-9", encoding="utf-8")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "approved"}})

    out = json.loads(
        await _tools(skills_dir)["check_skill_approval"].handler({"slug": "weather"})
    )
    assert out == {
        "ok": True,
        "status": "approved",
        "promoted": True,
        "slug": "weather",
    }
    assert (skills_dir / "weather" / "SKILL.md").is_file()
    assert not (skills_dir / "_pending" / "weather").exists()


def test_skill_promotion_tools_are_admin_only(tmp_path):
    """The promotion tools join only the admin profile — never the
    household/guest toolset (a household/voice turn drafts into pending but can
    never file or complete an approval)."""
    from solaris_chat.engine import profiles

    household, _deep, admin, guest, _lib, _enroll, _rec, _bus = (
        profiles.build_engine_clients(
            db_path=str(tmp_path / "solaris.db"),
            ollama_url="http://ollama",
            fast_model="m",
            thorough_model="m",
            soul_path="",
            skills_dir=str(tmp_path / "skills"),
            sb_mcp_url="http://sb/mcp",
            sb_mcp_token_path="/tmp/token",
        )
    )
    admin_names = set(admin._profile.toolbox.names())
    assert {"file_skill_approval", "check_skill_approval"} <= admin_names
    for client in (household, guest):
        names = set(client._profile.toolbox.names())
        assert "file_skill_approval" not in names
        assert "check_skill_approval" not in names
