"""Tests for the Radicale rights converge (option A, #997 / #1011).

The per-resident calendar lives under the RESIDENT's own principal
(`/<resident>/solaris/`), which Radicale's default `[rights] type = owner_only`
forbids the `solaris` DAV account from writing. `converge_radicale_rights`
rewrites the radicale pod manifest's `write-config` initContainer to emit a
`from_file` ruleset that keeps owner_only's guarantee AND grants `solaris`
write access to `<resident>/solaris` (and nothing else).

`_patched_radicale_rights_yaml` is the pure edit. These tests prove: the
`[rights]` flip, the inserted `/config/rights` heredoc + its body, that the
result is valid YAML AND a valid shell script (both heredoc `EOF` delimiters
land at column 0 after the `|` block scalar strips its indentation — a mis-
indented delimiter would never terminate the heredoc), idempotency, and the
no-anchor safety no-op.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# PyYAML isn't installed in the templates CI env; the yaml assertions run where
# it IS present (locally + the pod image at runtime). Skip when absent.
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
    return _load("solaris_pd_radicale", TEMPLATES / "solaris" / "post-deploy.py")


# The shape of the radicale pod manifest on the box: the write-config
# initContainer carries the whole config in a `| ` block-scalar heredoc, with
# `[rights] type = owner_only`. 8-space block indent (stripped by YAML at parse).
_RADICALE_YML = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "metadata:\n"
    "  name: radicale\n"
    "spec:\n"
    "  initContainers:\n"
    "  - name: write-config\n"
    "    image: docker.io/tomsquest/docker-radicale:latest\n"
    '    command: ["/bin/sh", "-c"]\n'
    "    args:\n"
    "      - |\n"
    "        cat > /config/config <<'EOF'\n"
    "        [server]\n"
    "        hosts = 0.0.0.0:5232\n"
    "\n"
    "        [auth]\n"
    "        type = ldap\n"
    "\n"
    "        [storage]\n"
    "        type = multifilesystem\n"
    "        filesystem_folder = /data/collections\n"
    "\n"
    "        [rights]\n"
    "        type = owner_only\n"
    "\n"
    "        [web]\n"
    "        type = internal\n"
    "\n"
    "        [logging]\n"
    "        level = info\n"
    "        EOF\n"
    "    volumeMounts:\n"
    "      - mountPath: /config\n"
    "        name: radicale-config\n"
    "  containers:\n"
    "  - name: radicale\n"
    "    image: docker.io/tomsquest/docker-radicale:latest\n"
)


def _init_script(text: str) -> str:
    """The write-config initContainer's shell script — the `| ` block scalar
    with its 8-space block indent stripped by YAML (== what /bin/sh runs)."""
    doc = yaml.safe_load(text)
    return doc["spec"]["initContainers"][0]["args"][0]


def test_flips_rights_block(pd):
    new, n = pd._patched_radicale_rights_yaml(_RADICALE_YML)
    assert n == 1 and new != _RADICALE_YML
    script = _init_script(new)
    assert "type = owner_only" not in script
    assert "type = from_file" in script
    assert "file = /config/rights" in script


def test_inserts_rights_heredoc_and_body(pd):
    new, _ = pd._patched_radicale_rights_yaml(_RADICALE_YML)
    script = _init_script(new)
    assert "cat > /config/rights <<'EOF'" in script
    # The owner base + the narrow solaris grant, verbatim.
    assert "[owner]" in script
    # Radicale substitutes {0} with the user regex's first CAPTURING group, so the
    # owner user MUST capture — a bare `.+` raises IndexError and 500s every call.
    assert "user: (.+)" in script
    assert "collection: {0}(/.*)?" in script
    assert "[solaris-subcal]" in script
    assert "user: solaris" in script
    assert "collection: [^/]+/solaris(/.*)?" in script
    assert script.count("permissions: RrWw") == 2


def test_result_is_valid_shell_heredoc(pd):
    # After the `| ` block scalar strips its indent, BOTH heredoc terminators
    # must sit at column 0 — else the `<<'EOF'` heredoc never terminates and the
    # initContainer hangs / writes garbage. Exactly two `EOF` delimiter lines
    # (config + rights), each with no leading whitespace.
    new, _ = pd._patched_radicale_rights_yaml(_RADICALE_YML)
    script = _init_script(new)
    eof_lines = [ln for ln in script.splitlines() if ln.strip() == "EOF"]
    assert len(eof_lines) == 2
    assert all(ln == "EOF" for ln in eof_lines)
    # Two heredoc openers, one per config file.
    assert script.count("<<'EOF'") == 2


def test_idempotent(pd):
    once, n1 = pd._patched_radicale_rights_yaml(_RADICALE_YML)
    twice, n2 = pd._patched_radicale_rights_yaml(once)
    assert n1 == 1
    assert n2 == 0  # already converged — a re-deploy is a no-op
    assert twice == once


def test_heals_drifted_rights_body(pd):
    # A manifest already on from_file but carrying an OUTDATED rights body (here
    # the bare `user: .+` that 500s) must be rewritten to the current desired
    # body — presence alone isn't enough, or a ruleset fix never reaches the box.
    once, _ = pd._patched_radicale_rights_yaml(_RADICALE_YML)
    drifted = once.replace("user: (.+)", "user: .+")
    assert drifted != once  # the drift is present
    healed, n = pd._patched_radicale_rights_yaml(drifted)
    assert n == 1
    assert healed == once  # converged back to the desired body
    assert "user: (.+)" in _init_script(healed)


def test_no_anchor_leaves_file_untouched(pd):
    # A manifest without the config heredoc / owner_only must be left byte-for-
    # byte unchanged (n==0) rather than corrupted.
    no_anchor = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: radicale\n"
    new, n = pd._patched_radicale_rights_yaml(no_anchor)
    assert n == 0
    assert new == no_anchor


# ── durable self-heal timer (#1023) ──────────────────────────────────────────
# A radicale re-render reverts the rights to owner_only until the next solaris
# deploy. The self-heal is a host-side systemd timer that re-runs this script in
# converge-only mode; the render_* helpers are pure, so they're unit-tested.


def test_rights_service_reruns_self_in_converge_only_mode(pd):
    service = pd.render_radicale_rights_service("/mnt/data/solarisbay/x.py")
    assert "Type=oneshot" in service
    assert "Environment=SOLARIS_CONVERGE_ONLY=1" in service
    assert "ExecStart=/usr/bin/env python3 /mnt/data/solarisbay/x.py" in service
    # Ordered after radicale so a co-boot restart doesn't race the pod's config.
    assert "After=radicale.service" in service


def test_rights_timer_fires_on_a_cadence_and_after_boot(pd):
    timer = pd.render_radicale_rights_timer()
    assert f"OnUnitActiveSec={pd.RADICALE_RIGHTS_TIMER_INTERVAL}" in timer
    assert "OnBootSec=" in timer
    # Persistent so a run missed while the box was off fires on the next boot.
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_converge_only_main_runs_just_the_converge(pd, monkeypatch):
    # SOLARIS_CONVERGE_ONLY short-circuits main() to the rights converge alone —
    # none of the deploy-time HA/token/restart wiring runs (the timer re-runs
    # this script, and must NOT re-adopt tokens or restart the pod).
    calls: list[str] = []
    monkeypatch.setenv("SOLARIS_CONVERGE_ONLY", "1")
    monkeypatch.setattr(
        pd, "converge_radicale_rights", lambda: calls.append("converge")
    )
    monkeypatch.setattr(
        pd,
        "install_voice_pipeline",
        lambda *a, **k: calls.append("voice"),
    )
    monkeypatch.setattr(pd, "restart_solaris", lambda *a, **k: calls.append("restart"))
    rc = pd.main()
    assert rc == 0
    assert calls == ["converge"]
