"""Tests for the `notify.solaris` write into HA's configuration.yaml (#1314).

`/api/ha/notify` and the phone's receive side both shipped, but nothing in Home
Assistant could send to them: no `notify.solaris` existed, so reaching the
endpoint meant hand-editing `configuration.yaml`. The post-deploy now writes the
`rest` notify platform entry itself.

`_patched_ha_notify_yaml` is the pure edit, and it is the only thing that can
break somebody's house, so it is what these tests pin down:

* a second `notify:` key is NEVER written — an existing list is merged into,
  and a `notify:` shape this code cannot extend (`!include`, a mapping) leaves
  the file byte-for-byte alone rather than guessing;
* the edit is idempotent across repeated deploys, and re-renders in place when
  CHAT_PORT changes;
* every byte outside our marked block survives — `configuration.yaml` is
  seed-only (servicebay#2597) and carries an `auth_oidc` `client_secret`, so a
  re-render, or a redacted read-modify-write, would lock the operator out of HA;
* `category` and `urgency` really ride the platform's `data_template`, which is
  what keeps a reminder and a house notice on separate phone channels (#1280).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

yaml = pytest.importorskip("yaml")

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solaris_pd_ha_notify", TEMPLATES / "solaris" / "post-deploy.py")


# The box's file as it stands today: no `notify:`, no `rest_command:`, and an
# `auth_oidc` block whose `client_secret` must come through untouched.
_GREENFIELD = (
    "# Loaded by Home Assistant on start.\n"
    "default_config:\n"
    "\n"
    "auth_oidc:\n"
    "  client_id: home-assistant\n"
    "  client_secret: s3cr3t-do-not-touch\n"
    "  discovery_url: https://auth.example.invalid/.well-known/openid-configuration\n"
    "\n"
    "frontend:\n"
    "  themes: !include_dir_merge_named themes\n"
)

_WITH_NOTIFY_LIST = (
    "default_config:\n"
    "\n"
    "notify:\n"
    "  - name: hausmail\n"
    "    platform: smtp\n"
    "    recipient: mdopp@example.invalid\n"
    "\n"
    "sensor:\n"
    "  - platform: time_date\n"
    "    display_options:\n"
    "      - time\n"
)


def _top_level_keys(text: str) -> list[str]:
    return [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if line[:1].strip() and not line.startswith("#") and ":" in line
    ]


class _HALoader(yaml.SafeLoader):
    """HA's `!include*` / `!secret` tags are not PyYAML's — keep them as text."""


_HALoader.add_multi_constructor(
    "!", lambda loader, suffix, node: f"!{suffix} {node.value}"
)


def _notify_entries(text: str) -> list[dict]:
    return yaml.load(text, Loader=_HALoader)["notify"]


def test_greenfield_appends_a_notify_section(pd):
    out, outcome = pd._patched_ha_notify_yaml(_GREENFIELD, "8787")
    assert outcome == "appended"
    entries = _notify_entries(out)
    assert [e["name"] for e in entries] == ["solaris"]
    entry = entries[0]
    assert entry["platform"] == "rest"
    assert entry["resource"] == "http://127.0.0.1:8787/api/ha/notify"
    assert entry["method"] == "POST_JSON"
    # The 1:1 field mapping onto the endpoint's closed key set — the reason no
    # code changes on either side.
    assert entry["message_param_name"] == "body"
    assert entry["title_param_name"] == "title"
    assert entry["target_param_name"] == "target"


def test_category_and_urgency_ride_data_template(pd):
    """They must reach the endpoint FLAT and from the caller's own `data:`.

    `category` picks the notification channel on the phone (#1280), so losing it
    would merge reminders into house notices. HA's legacy notify always hands the
    platform the caller's `data` (as None when absent) and the rest platform
    renders each `data_template` value against it, merging the result flat — so
    `(data or {}).get(…)` yields the caller's value or the endpoint's default.
    """
    out, _ = pd._patched_ha_notify_yaml(_GREENFIELD, "8787")
    template = _notify_entries(out)[0]["data_template"]
    assert template == {
        "category": "{{ (data or {}).get('category', 'house') }}",
        "urgency": "{{ (data or {}).get('urgency', 'normal') }}",
    }


def test_existing_notify_list_is_merged_not_clobbered(pd):
    """The operator's own notifier survives, and there is only ONE `notify:`.

    A second mapping key of the same name silently displaces the first — the
    exact trap this whole change exists to keep the operator out of.
    """
    out, outcome = pd._patched_ha_notify_yaml(_WITH_NOTIFY_LIST, "8787")
    assert outcome == "merged"
    assert _top_level_keys(out).count("notify") == 1
    names = [e["name"] for e in _notify_entries(out)]
    assert names == ["hausmail", "solaris"]
    assert _notify_entries(out)[0]["recipient"] == "mdopp@example.invalid"
    # The neighbouring key keeps its own content, unindented and unmoved.
    assert "sensor:\n  - platform: time_date\n" in out


def test_untouchable_notify_shapes_leave_the_file_alone(pd):
    """`notify: !include …` / a mapping is not ours to rewrite — we skip.

    Not shipping the feature is recoverable; mangling the operator's live
    notification config is not.
    """
    for value in ("!include notify.yaml", "!include_dir_merge_list notify/", "[]"):
        src = f"default_config:\n\nnotify: {value}\n"
        out, outcome = pd._patched_ha_notify_yaml(src, "8787")
        assert outcome == "foreign_notify"
        assert out == src


def test_repeated_deploys_are_idempotent(pd):
    for src in (_GREENFIELD, _WITH_NOTIFY_LIST):
        once, _ = pd._patched_ha_notify_yaml(src, "8787")
        twice, _ = pd._patched_ha_notify_yaml(once, "8787")
        assert twice == once
        assert len(_notify_entries(twice)) == len(_notify_entries(once))


def test_a_changed_chat_port_is_re_rendered_in_place(pd):
    once, _ = pd._patched_ha_notify_yaml(_GREENFIELD, "8787")
    moved, _ = pd._patched_ha_notify_yaml(once, "9999")
    entries = _notify_entries(moved)
    assert len(entries) == 1
    assert entries[0]["resource"] == "http://127.0.0.1:9999/api/ha/notify"


def test_everything_outside_our_block_survives_byte_for_byte(pd):
    """The file is seed-only and holds an OIDC `client_secret` (servicebay#2597).

    Re-rendering it, or a read-modify-write through a redacting reader, would
    write `<redacted>` back as the secret and lock the operator out of HA. The
    edit must be additive: strip our block from the result and the original file
    must come back exactly.
    """
    for src in (_GREENFIELD, _WITH_NOTIFY_LIST):
        out, _ = pd._patched_ha_notify_yaml(src, "8787")
        assert "s3cr3t-do-not-touch" in out or "client_secret" not in src
        rest = pd._strip_managed_notify(out.splitlines())
        assert rest is not None
        assert "\n".join(rest).rstrip("\n") == src.rstrip("\n")


def test_a_notify_the_operator_adds_later_absorbs_our_block(pd):
    """Our standalone block migrates into a `notify:` that appears afterwards.

    Otherwise the next deploy would leave two `notify:` keys — one of them
    silently dead.
    """
    standalone, _ = pd._patched_ha_notify_yaml(_GREENFIELD, "8787")
    operator_added = standalone.replace(
        "default_config:\n",
        "default_config:\n\nnotify:\n  - name: hausmail\n    platform: smtp\n",
        1,
    )
    out, outcome = pd._patched_ha_notify_yaml(operator_added, "8787")
    assert outcome == "merged"
    assert _top_level_keys(out).count("notify") == 1
    assert [e["name"] for e in _notify_entries(out)] == ["hausmail", "solaris"]


def test_a_half_edited_block_is_never_rewritten(pd):
    """A begin marker with no end marker means somebody is mid-edit — hands off."""
    src = _GREENFIELD + pd.NOTIFY_MARK_BEGIN + "\nnotify:\n  - name: solaris\n"
    out, outcome = pd._patched_ha_notify_yaml(src, "8787")
    assert outcome == "unterminated_marker"
    assert out == src
