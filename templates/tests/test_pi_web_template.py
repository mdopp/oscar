"""The `pi-web` template (#1357): where it may reach, and which model it uses.

Three things here have no failure mode that looks like a failure, which is why
they are asserted rather than trusted:

* **The network shape.** ADR 0007 keeps a closed carve-out list and pi-web is
  not on it, so the pod is isolated and addresses llama-server as
  `host.containers.internal`. A `127.0.0.1` that slipped in would work in every
  test and reach nothing on the box — the model picker would merely be empty.
* **The LAN block.** PI WEB has no login of its own; without
  `blockLanAccess` on its published port the whole Authelia gate is one
  `curl http://<box>:8504/` away from being irrelevant, and nothing about a
  working install would say so.
* **That nothing here takes the coding lease (#1392).** PI WEB runs around the
  clock and answers on whatever llama-server serves; the lease belongs to the
  Solaris model tile. A lease unit that came back, or a post-deploy that stopped
  the pod again, would look exactly like a working install — until `pi.<domain>`
  was dead after a reboot, or the household assistant was slow for four hours.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
PI_WEB = TEMPLATES / "pi-web"
ROOT = TEMPLATES.parent


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("pi_web_pd", PI_WEB / "post-deploy.py")


@pytest.fixture(scope="module")
def template_text() -> str:
    return (PI_WEB / "template.yml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pod(template_text) -> dict:
    # The Mustache tags that stand alone as a YAML value (`hostPort:
    # {{PI_WEB_PORT}}`) are not valid YAML until they are rendered, so render
    # the defaults in first — which also proves the defaults produce a
    # parseable manifest.
    variables = json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))
    rendered = template_text
    for key, spec in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(spec["default"]))
    for key, value in (("DATA_DIR", "/mnt/data/stacks"), ("PUBLIC_DOMAIN", "example")):
        rendered = rendered.replace("{{" + key + "}}", value)
    return yaml.safe_load(rendered)


@pytest.fixture(scope="module")
def variables() -> dict:
    return json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))


# ── the manifest ────────────────────────────────────────────────────────────


def test_pod_renders_and_declares_its_port(pod):
    assert pod["metadata"]["name"] == "pi-web"
    assert pod["metadata"]["annotations"]["servicebay.ports"] == "8504/tcp"


def test_pod_is_not_on_host_networking(pod):
    """ADR 0007 Decision 2: the carve-out list is closed and pi-web is new."""
    assert "hostNetwork" not in pod["spec"]


def test_published_port_has_a_host_port(pod):
    """An isolated pod with neither hostNetwork nor a hostPort is silently
    unreachable — nginx would proxy to nothing."""
    web = next(c for c in pod["spec"]["containers"] if c["name"] == "web")
    published = web["ports"][0]
    assert published["containerPort"] == 8504
    assert published["hostPort"] == 8504


def test_both_containers_keep_tini_as_entrypoint(pod):
    """`command:` in kube YAML replaces ENTRYPOINT; the sessions' children
    would then never be reaped."""
    for container in pod["spec"]["containers"]:
        assert "command" not in container
        assert container["args"] in (["pi-web-sessiond"], ["pi-web-server"])


def test_the_two_processes_share_the_state_volume(pod):
    """web reaches sessiond over a unix socket on /data — a container missing
    that mount comes up and answers nothing."""
    for container in pod["spec"]["containers"]:
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        assert {"/data", "/workspace"} <= mounts, container["name"]


def test_data_perms_are_opened_before_the_nonroot_containers_start(pod):
    """`DirectoryOrCreate` leaves the host path owned by userns-root; sessiond
    and web both run as the image's non-root `USER node`, so without this
    sessiond's first write (claiming its state socket) hits EACCES on every
    start — box-verified against #1358."""
    init = pod["spec"]["initContainers"]
    perms = next(c for c in init if c["name"] == "pi-web-data-perms")
    assert perms["securityContext"]["runAsUser"] == 0
    mounts = {m["mountPath"] for m in perms["volumeMounts"]}
    assert {"/data", "/workspace"} <= mounts
    assert "/data" in perms["command"] and "/workspace" in perms["command"]


def test_template_references_public_domain(template_text):
    """Without a reference the assembler never injects PUBLIC_DOMAIN and the
    subdomain's proxy host is dropped with nothing in the install log."""
    assert "{{PUBLIC_DOMAIN}}" in template_text


def test_nothing_addresses_llama_by_loopback_or_lan(template_text, pd):
    assert "127.0.0.1:11435" not in template_text
    assert "{{LAN_IP}}" not in template_text
    provider = pd.models_document("11435")["providers"][pd.PROVIDER_ID]
    assert provider["baseUrl"] == "http://host.containers.internal:11435/v1"


def test_the_model_server_is_never_reached_through_its_domain(pd, template_text):
    """`llama.<domain>` is Authelia-gated: a service using it meets a login."""
    document = json.dumps(pd.models_document("11435"))
    assert "llama." not in document.replace("llama.cpp", "")
    assert "https://" not in document


# ── the route and the LAN block ─────────────────────────────────────────────


def test_port_blocks_lan_access(variables):
    """PI WEB has no login of its own; the proxy host is the only way in."""
    assert variables["PI_WEB_PORT"]["blockLanAccess"] is True


def test_subdomain_is_internal_and_behind_authelia(variables):
    sub = variables["PI_WEB_SUBDOMAIN"]
    assert sub["type"] == "subdomain"
    assert sub["default"] == "pi"
    assert sub["exposure"] == "internal"
    assert sub["proxyPort"] == "PI_WEB_PORT"
    assert sub["proxyConfig"]["advanced_config"].startswith("__authelia_forward_auth__")


def test_route_upgrades_websockets(variables):
    """A session's transcript arrives over a WebSocket; without the upgrade the
    UI loads and then never shows an answer."""
    assert variables["PI_WEB_SUBDOMAIN"]["proxyConfig"]["allow_websocket_upgrade"]


def test_every_variable_carries_a_description(variables):
    for key, spec in variables.items():
        assert spec.get("description", "").strip(), key


# ── the models.json the Pi agent reads ──────────────────────────────────────


def test_provider_speaks_openai_completions(pd):
    provider = pd.models_document("11435")["providers"][pd.PROVIDER_ID]
    assert provider["api"] == "openai-completions"
    # llama-server takes neither of these; asking turns every call into a 400.
    assert provider["compat"] == {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
    }


def test_provider_carries_a_placeholder_key(pd):
    """Not a secret — llama-server has no auth. Pi hides models whose provider
    has no auth configured at all, so the placeholder is what makes them show."""
    provider = pd.models_document("11435")["providers"][pd.PROVIDER_ID]
    assert provider["apiKey"] == "llama"


def test_both_loaded_aliases_are_offered(pd):
    provider = pd.models_document("11435")["providers"][pd.PROVIDER_ID]
    ids = [m["id"] for m in provider["models"]]
    assert ids == [pd.CODING_ALIAS, pd.HOUSEHOLD_ALIAS]


def test_the_coding_model_matches_the_leased_profile(pd):
    """The alias and window are llama's coding profile, not a guess: a model
    entry naming something llama-server does not serve is an empty picker."""
    llama = _load("llama_pd_for_pi_web", TEMPLATES / "llama" / "post-deploy.py")
    assert pd.CODING_ALIAS == llama.CODING_PROFILE["alias"]
    assert pd.CODING_CONTEXT == int(llama.CODING_PROFILE["context_length"])


def test_the_household_model_is_left_alone(pd):
    """#1325: the household model stays e4b — pi-web leases, it does not swap."""
    llama_vars = json.loads(
        (TEMPLATES / "llama" / "variables.json").read_text(encoding="utf-8")
    )
    assert pd.HOUSEHOLD_ALIAS == llama_vars["LLAMA_MODEL_ALIAS"]["default"]


def test_models_json_lands_where_the_container_reads_it(pd):
    """The image sets PI_CODING_AGENT_DIR=/data/pi-agent and the pod mounts
    {{DATA_DIR}}/pi-web/data there."""
    assert pd.agent_dir("/mnt/data/stacks") == "/mnt/data/stacks/pi-web/data/pi-agent"
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    assert "PI_CODING_AGENT_DIR=/data/pi-agent" in dockerfile


def test_write_models_json_is_idempotent(pd, tmp_path):
    assert pd.write_models_json(str(tmp_path), "11435")
    path = tmp_path / "pi-web" / "data" / "pi-agent" / "models.json"
    first = path.read_text(encoding="utf-8")
    assert pd.write_models_json(str(tmp_path), "11435")
    assert path.read_text(encoding="utf-8") == first
    assert json.loads(first) == pd.models_document("11435")


# ── the one-time release of the retired unit's window (#1392) ───────────────


def test_a_window_left_by_the_retired_unit_is_recognised_as_ours(pd):
    """The unit is removed by this upgrade, so nothing else would ever give
    that card back — the box would sit on Qwen until the TTL ran out."""
    for state in ("ready", "preparing"):
        assert pd.is_own_stale_window(200, {"state": state, "holder": "pi-web"})


def test_a_window_that_is_not_ours_is_left_alone(pd):
    """The model tile holds the lease under its own holder now; closing that
    one would take Qwen away from the operator who just asked for it."""
    assert not pd.is_own_stale_window(200, {"state": "ready", "holder": "widget"})
    assert not pd.is_own_stale_window(200, {"state": "ready", "holder": ""})
    assert not pd.is_own_stale_window(200, {"state": "none", "holder": ""})
    assert not pd.is_own_stale_window(0, {})
    assert not pd.is_own_stale_window(503, {"state": "ready", "holder": "pi-web"})


def test_the_lease_endpoint_is_the_engine_on_loopback(pd):
    """It has no token: reachability is the authorisation, which is why only a
    host-side script can reach it and never this pod."""
    assert pd.lease_url("8787") == "http://127.0.0.1:8787/api/model-lease"


def test_release_deletes_exactly_once_and_only_our_own(pd, monkeypatch):
    calls: list[tuple] = []

    def fake(url, payload=None, method="GET", timeout=10.0):
        calls.append((method, payload))
        return 200, {"state": "ready", "holder": "pi-web"}

    monkeypatch.setattr(pd, "http_request", fake)
    pd.release_own_lease("8787")
    assert [c[0] for c in calls] == ["GET", "DELETE"]
    assert calls[1][1] == {"holder": "pi-web"}


def test_release_asks_before_it_deletes(pd, monkeypatch):
    """Idempotent: the ordinary deploy finds no window of ours and sends no
    DELETE at all — a blind DELETE would cancel the model tile's lease on
    every single deploy."""
    calls: list[str] = []

    def fake(url, payload=None, method="GET", timeout=10.0):
        calls.append(method)
        return 200, {"state": "ready", "holder": "widget"}

    monkeypatch.setattr(pd, "http_request", fake)
    pd.release_own_lease("8787")
    assert calls == ["GET"]


# ── the image ───────────────────────────────────────────────────────────────


def test_the_image_is_built_here_because_upstream_publishes_none(pod):
    web = next(c for c in pod["spec"]["containers"] if c["name"] == "web")
    assert web["image"] == "ghcr.io/mdopp/solaris-pi-web:latest"
    matrix = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "build-images.yml").read_text(
            encoding="utf-8"
        )
    )["jobs"]["build"]["strategy"]["matrix"]["include"]
    entry = next(e for e in matrix if e["image"] == "solaris-pi-web")
    assert entry["context"] == "pi-web"


def test_the_pi_web_version_is_pinned(pd):
    """`latest` would move the agent runtime under a running session on any
    redeploy; the bump is a commit."""
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    pinned = [
        line
        for line in dockerfile.splitlines()
        if line.startswith("ARG PI_WEB_VERSION=")
    ]
    assert len(pinned) == 1, pinned
    assert pinned[0] != "ARG PI_WEB_VERSION=latest"


def test_pi_web_is_not_in_the_household_stack():
    """The stack is the household assistant; pi-web is a developer tool on the
    same box, like paperless."""
    stack = yaml.safe_load(
        (ROOT / "stacks" / "solarisbay" / "stack.yml").read_text(encoding="utf-8")
    )
    assert "pi-web" not in stack["spec"]["templates"]


# ── runs around the clock, and leases nothing (#1392) ───────────────────────


PLATFORM_KUBE = (
    "[Kube]\n"
    "Yaml=pi-web.yml\n"
    "AutoUpdate=registry\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target\n"
    "[Service]\n"
    "TimeoutStartSec=600\n"
    "Restart=on-failure\n"
    "\n"
    "[Unit]\n"
    "StartLimitIntervalSec=0\n"
)

# What #1373 left behind on this box: the same unit with its `[Install]`
# section stripped out. ServiceBay only rewrites the file when the rendered
# spec changed, so an upgrade can meet exactly this.
STRIPPED_KUBE = PLATFORM_KUBE.replace("[Install]\nWantedBy=default.target\n", "")


def _written_strings() -> list[str]:
    """Every string literal the post-deploy could write out — docstrings, which
    describe the retired unit, excluded."""
    tree = ast.parse((PI_WEB / "post-deploy.py").read_text(encoding="utf-8"))
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
    ]


def test_no_lease_unit_is_rendered_any_more(pd):
    """The whole point of #1392: PI WEB must not take the coding lease by being
    started. A unit that came back would do it on every boot, silently."""
    for literal in _written_strings():
        for banned in ("BindsTo", "WantedBy=pi-web.service", "ExecStart="):
            assert banned not in literal, literal
    for gone in ("render_lease_unit", "install_lease_unit", "install_lease_script"):
        assert not hasattr(pd, gone), gone


def test_the_post_deploy_never_stops_the_pod(pd):
    """#1373 stopped pi-web after every deploy, which is why `pi.<domain>` was
    dead. Nothing may issue that stop again."""
    source = (PI_WEB / "post-deploy.py").read_text(encoding="utf-8")
    assert '"stop", POD_UNIT' not in source
    for gone in ("restore_run_state", "state_before", "record_run_state"):
        assert not hasattr(pd, gone), gone


def test_the_autostart_section_is_left_in_place(pd):
    """ServiceBay's `[Install] WantedBy=default.target` is what Quadlet turns
    into the boot link; stripping it is what kept PI WEB off (#1373)."""
    source = (PI_WEB / "post-deploy.py").read_text(encoding="utf-8")
    assert "strip_boot_install" not in source
    assert pd.add_boot_install(PLATFORM_KUBE) == PLATFORM_KUBE


def test_a_kube_unit_stripped_by_the_old_template_gets_its_autostart_back(pd):
    """The upgrade path on this box: the file on disk has no `[Install]`, and
    ServiceBay does not rewrite it unless the spec changed."""
    restored = pd.add_boot_install(STRIPPED_KUBE)
    assert "[Install]" in restored
    assert "WantedBy=default.target" in restored
    assert "Yaml=pi-web.yml" in restored
    assert "StartLimitIntervalSec=0" in restored
    # Idempotent — every later deploy must leave the file alone rather than
    # rewrite it and reload the generator.
    assert pd.add_boot_install(restored) == restored


def test_restoring_the_autostart_reloads_the_generator_and_only_when_needed(
    pd, tmp_path, monkeypatch
):
    quadlet = tmp_path / ".config" / "containers" / "systemd"
    quadlet.mkdir(parents=True)
    (quadlet / "pi-web.kube").write_text(STRIPPED_KUBE, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[list[str]] = []
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    pd.restore_boot_autostart()
    assert "WantedBy=default.target" in (quadlet / "pi-web.kube").read_text(
        encoding="utf-8"
    )
    # Without the reload the generator keeps the unit that has no boot link.
    assert ["systemctl", "--user", "daemon-reload"] in calls

    calls.clear()
    pd.restore_boot_autostart()
    assert calls == []


def test_the_pod_is_started_by_the_deploy(pd, monkeypatch):
    """An upgraded box has pi-web stopped; without this it stays stopped until
    somebody opens the ServiceBay UI."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    pd.start_pod()
    assert calls == [["systemctl", "--user", "start", "pi-web.service"]]


def test_the_lease_unit_is_stopped_disabled_and_removed(pd, tmp_path, monkeypatch):
    """Removing the unit file alone is not enough: it is `WantedBy=`
    pi-web.service, and an enabled leftover would be started with a PI WEB that
    now runs permanently — the exact regression this issue is about."""
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    unit_file = units / "pi-web-model-lease.service"
    unit_file.write_text("[Unit]\nBindsTo=pi-web.service\n", encoding="utf-8")
    script = tmp_path / "data" / "pi-web" / "pi-web-lease.py"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[list[str]] = []
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    pd.retire_lease_unit(str(tmp_path / "data"))
    assert ["systemctl", "--user", "stop", "pi-web-model-lease.service"] in calls
    assert ["systemctl", "--user", "disable", "pi-web-model-lease.service"] in calls
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert not unit_file.exists()
    # The script copy is what the unit ran; leaving an executable that acquires
    # the lease behind is half a retirement.
    assert not script.exists()


def test_retiring_a_lease_unit_that_is_already_gone_is_a_no_op(
    pd, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: None)
    pd.retire_lease_unit(str(tmp_path / "data"))


def test_the_start_on_boot_knob_is_gone(variables):
    """A knob that switched PI WEB off at boot has no meaning once the service
    is meant to run permanently — and an operator setting it would get a dead
    `pi.<domain>` with no hint why."""
    assert "PI_WEB_START_ON_BOOT" not in variables
    assert "PI_WEB_LEASE_TTL_SECONDS" not in variables


def test_the_readme_says_where_qwen_comes_from(variables):
    """The operator-facing sentence: nothing in the template asks for Qwen, so
    the README has to say who does."""
    readme = (PI_WEB / "README.md").read_text(encoding="utf-8")
    assert "Modell-Kachel" in readme
    assert "Haushaltsmodell" in readme
