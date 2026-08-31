"""A shipped skill must not drive storage nothing in the engine can touch.

#1293: `audit-query` (`/audit`) and `debug-set` (`/debug`) instructed the model
to SELECT and UPDATE `cloud_audit` / `system_settings` in `solaris.db`. No tool
in `solaris-chat` reads or writes either table (they are cloud-gateway leftovers
from before Solaris went local-model-only), so `/debug on` confirmed a debug mode
that was never persisted and `/audit` reported "nothing went to the cloud" as a
finding rather than as dead code. Both are retired; this pins that no pack
reintroduces a command whose only backing is a table nobody serves.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "templates" / "solaris" / "skills"

# Tables the baseline migration still creates but no engine code reads or writes.
UNSERVED_TABLES = ("cloud_audit", "system_settings")


def _bodies() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in SKILLS.glob("*/*/SKILL.md")}


def test_retired_cloud_audit_skills_are_gone():
    assert not (SKILLS / "household" / "audit-query").exists()
    assert not (SKILLS / "household" / "debug-set").exists()


def test_no_skill_drives_a_table_no_tool_serves():
    for path, text in _bodies().items():
        for table in UNSERVED_TABLES:
            assert table not in text, f"{path.relative_to(ROOT)} references {table}"


def test_no_skill_declares_the_retired_commands():
    for path, text in _bodies().items():
        for cmd in ("command: /audit", "command: /debug"):
            assert cmd not in text, f"{path.relative_to(ROOT)} declares {cmd}"
