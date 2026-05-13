"""Validation: protects routing description, blocks admin escalations."""

from __future__ import annotations

import pytest

from oscar_skill_author.validation import (
    ADMIN_TAG,
    ValidationError,
    parse_frontmatter,
    validate_edit,
)


GOOD = """---
name: oscar-timer
description: Use when the user wants to set a timer.
version: 0.2.0
metadata:
  hermes:
    tags: [time, phase-0]
    related_skills: [oscar-alarm]
---

# OSCAR — timer

body unchanged
"""

GOOD_EDITED_BODY = """---
name: oscar-timer
description: Use when the user wants to set a timer.
version: 0.3.0
metadata:
  hermes:
    tags: [time, phase-0]
    related_skills: [oscar-alarm]
---

# OSCAR — timer

body edited but frontmatter same
"""


def test_parse_frontmatter_returns_known_fields():
    p = parse_frontmatter(GOOD)
    assert p["name"] == "oscar-timer"
    assert p["description"].startswith("Use when")
    assert "time" in p["tags"]
    assert p["related_skills"] == ("oscar-alarm",)


def test_parse_frontmatter_empty_when_no_block():
    assert parse_frontmatter("no frontmatter here") == {}


def test_validate_new_skill_requires_name_and_description():
    bad = "---\nname:\ndescription: blah\n---\n"
    with pytest.raises(ValidationError):
        validate_edit(bad, current=None)


def test_validate_edit_protects_description():
    changed = GOOD_EDITED_BODY.replace(
        "Use when the user wants to set a timer.",
        "Use when the user is hungry.",
    )
    with pytest.raises(ValidationError, match="'description'"):
        validate_edit(changed, current=GOOD)


def test_validate_edit_protects_name():
    changed = GOOD_EDITED_BODY.replace("oscar-timer", "oscar-timer-v2")
    with pytest.raises(ValidationError, match="'name'"):
        validate_edit(changed, current=GOOD)


def test_validate_edit_allows_body_change_with_matching_frontmatter():
    validate_edit(GOOD_EDITED_BODY, current=GOOD)


def test_validate_blocks_admin_tag_creation():
    body = (
        "---\nname: nope\ndescription: tries to be admin.\n"
        "metadata:\n  hermes:\n    tags: [admin]\n---\n"
    )
    with pytest.raises(ValidationError, match="admin"):
        validate_edit(body, current=None)


def test_validate_blocks_admin_tag_on_edit_target():
    admin_existing = (
        "---\nname: oscar-debug-set\ndescription: admin.\n"
        "metadata:\n  hermes:\n    tags: [debug, admin]\n---\n"
    )
    proposed = admin_existing.replace("description: admin.", "description: admin.")
    with pytest.raises(ValidationError, match="admin"):
        validate_edit(proposed, current=admin_existing)


def test_validate_blocks_adding_admin_tag_during_edit():
    proposed = GOOD.replace("tags: [time, phase-0]", "tags: [time, phase-0, admin]")
    with pytest.raises(ValidationError, match="admin"):
        validate_edit(proposed, current=GOOD)


def test_validate_constant_protected_field_list():
    # Guard against accidental loosening of the contract.
    from oscar_skill_author.validation import PROTECTED_FRONTMATTER_FIELDS

    assert set(PROTECTED_FRONTMATTER_FIELDS) == {"name", "description"}
    assert ADMIN_TAG == "admin"
