"""Absolute vault paths round-trip between note_write and notes_read (#1173).

The skills instruct the absolute path (`/opt/data/notes/...`), so both tools must
reduce it to the same vault-relative file — otherwise the problem-summarizer's
read-merge-write cycle never finds what it wrote and overwrites the KB weekly.
"""

from __future__ import annotations

import json

from solaris_chat import notes_search
from solaris_chat.engine.tools.notes import build_notes_tools

_KB = "knowledge-base/troubleshooting.md"


def _handlers(vault: str):
    tools = {
        t.name: t.handler
        for t in build_notes_tools(vault, lambda: notes_search.SHARED_UID)
    }
    return tools["note_write"], tools["notes_read"]


async def test_absolute_path_write_then_read(tmp_path):
    root = tmp_path / "notes"
    write, read = _handlers(str(root))
    absolute = f"{root}/{_KB}"

    written = json.loads(await write({"path": absolute, "content": "# KB\n\nHeizung"}))
    assert written["written"] == _KB
    assert (root / _KB).is_file()
    # No `<vault>/opt/data/notes/...` shadow tree from a half-stripped path.
    assert sorted(p.name for p in root.rglob("*.md")) == ["troubleshooting.md"]

    out = json.loads(await read({"path": absolute}))
    assert out["path"] == _KB
    assert "Heizung" in out["content"]


async def test_relative_and_absolute_read_same_note(tmp_path):
    root = tmp_path / "notes"
    write, read = _handlers(str(root))
    await write({"path": _KB, "content": "# KB\n\nHeizung"})

    relative = json.loads(await read({"path": _KB}))
    absolute = json.loads(await read({"path": f"{root}/{_KB}"}))
    assert relative == absolute
    assert "Heizung" in relative["content"]


async def test_absolute_path_outside_vault_stays_inside(tmp_path):
    root = tmp_path / "notes"
    write, _ = _handlers(str(root))
    outside = tmp_path / "elsewhere" / "secret.md"

    await write({"path": str(outside), "content": "# X\n\nnicht hier"})

    assert not outside.exists()
    assert list(root.rglob("secret.md"))
