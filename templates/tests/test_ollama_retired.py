"""Ollama is retired (#1332) — and stays retired.

Two failure modes this pins, both of which have bitten this repo before:

1. **A half-retirement.** A template body removed but still listed as a stack
   member or a dependency is something offered that does nothing — the exact
   shape of #1293, where two deleted skills kept working in production. So:
   nothing may install `ollama`, and the retired directory must not grow a
   `template.yml` back.

2. **Over-retirement.** The `/ollama` surface on the Solaris Engine is the wire
   protocol Home Assistant's conversation integration and the voice-gatekeeper
   speak. It is not the service. Removing it would take Solaris out of HA's
   Assist pipeline, so the facade wiring and the HA `ollama` config entry the
   post-deploy maintains are asserted present.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
OLLAMA = TEMPLATES / "ollama"
REPO = TEMPLATES.parent


def test_the_ollama_template_installs_nothing():
    for gone in ("template.yml", "variables.json", "post-deploy.py", "migrations"):
        assert not (OLLAMA / gone).exists(), f"templates/ollama/{gone} came back"


def test_the_directory_keeps_a_tombstone_that_says_so():
    """The README stays where somebody would go looking for the template —
    a directory that simply vanished tells the next reader nothing."""
    readme = (OLLAMA / "README.md").read_text(encoding="utf-8")
    assert "RETIRED" in readme
    assert "llama" in readme
    # It has to say what replaced it and what to do with the installed service,
    # or the retirement is a dead end.
    assert "delete_service" in readme
    assert "/ollama" in readme


def test_no_template_declares_ollama_a_dependency():
    for template in TEMPLATES.iterdir():
        yml = template / "template.yml"
        if not yml.is_file():
            continue
        for line in yml.read_text(encoding="utf-8").splitlines():
            if "servicebay.dependencies" in line:
                deps = [d.strip() for d in line.split('"')[1].split(",")]
                assert "ollama" not in deps, f"{template.name} still depends on ollama"


def test_the_stack_no_longer_installs_it():
    stack = (REPO / "stacks" / "solarisbay" / "stack.yml").read_text(encoding="utf-8")
    members = stack.split("templates:")[1].split("]")[0]
    assert "ollama" not in members
    assert "llama" in members


def test_the_solaris_pod_carries_no_ollama_url():
    """`OLLAMA_URL` pointed at 127.0.0.1:11434. Left in place it reads like a
    working fallback and would make the engine wait out a dead socket."""
    pod = (TEMPLATES / "solaris" / "template.yml").read_text(encoding="utf-8")
    env_names = [
        line.split("name:", 1)[1].strip()
        for line in pod.splitlines()
        if line.strip().startswith("- name:")
    ]
    assert "OLLAMA_URL" not in env_names
    assert "LLAMA_EMBED_URL" in env_names

    variables = json.loads(
        (TEMPLATES / "solaris" / "variables.json").read_text(encoding="utf-8")
    )
    assert "OLLAMA_URL" not in variables
    assert variables["LLAMA_EMBED_URL"]["default"] == "http://127.0.0.1:11436"


def test_the_schema_bump_carries_a_runnable_migration():
    """A pod-env change needs the pod recreated, so the version moves — and
    every migration script on this box is EXECUTED, not imported.

    The mode is read out of git, not off the filesystem: this repo runs with
    `core.fileMode=false`, so a 755 working copy can still be committed 644 and
    ship un-runnable. That is exactly how #1344's llama hop shipped."""
    pod = (TEMPLATES / "solaris" / "template.yml").read_text(encoding="utf-8")
    assert 'servicebay.schema-version: "2"' in pod
    rel = "templates/solaris/migrations/v1-to-v2.py"
    assert (REPO / rel).is_file()
    out = subprocess.run(
        ["git", "ls-files", "-s", rel],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("100755"), f"migration is not executable in git: {out!r}"


def test_the_protocol_facade_survives_the_retirement():
    """The gatekeeper and HA both speak the protocol facade on the engine."""
    pod = (TEMPLATES / "solaris" / "template.yml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:{{CHAT_PORT}}/ollama" in pod


def test_the_post_deploy_still_wires_has_ollama_config_entry():
    """HA's conversation integration is named `ollama` and is how Solaris is
    the Assist agent. Retiring the service must not touch it."""
    post = (TEMPLATES / "solaris" / "post-deploy.py").read_text(encoding="utf-8")
    assert 'flow(\n        token, "ollama"' in post or '"ollama", [{"url"' in post
    assert "reassert_ollama_api_key" in post
