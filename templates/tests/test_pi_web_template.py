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
        # A `secret` the operator leaves blank has no default at all; ServiceBay
        # prunes the env entry it renders to, which is the case the credential
        # init has to survive.
        rendered = rendered.replace("{{" + key + "}}", str(spec.get("default", "")))
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


def test_every_container_keeps_tini_as_entrypoint(pod):
    """`command:` in kube YAML replaces ENTRYPOINT; the sessions' children
    would then never be reaped."""
    for container in pod["spec"]["containers"]:
        assert "command" not in container
        assert container["args"] in (
            ["pi-web-sessiond"],
            ["pi-web-server"],
            ["pi-web-autoloop"],
        )


def test_the_processes_share_the_state_volume(pod):
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


def test_the_agent_kit_path_survives_a_plain_upgrade(template_text, variables, pod):
    """#1403: the kit path was `PI_WEB_AGENT_KIT_DIR` for one release. ServiceBay
    does not apply the default of a *newly added* variable when an existing
    service is upgraded (servicebay#2913), so a plain `install_template pi-web`
    rendered the tag empty, the hostPath became `/agent-cli`, and the pod
    crash-looped on a read-only root — while the install job reported `done`.
    A bare Mustache hostPath is only safe for a platform variable; an override,
    if one ever comes back, has to go through the post-deploy instead."""
    assert "PI_WEB_AGENT_KIT_DIR" not in variables
    assert "PI_WEB_AGENT_KIT_DIR" not in template_text

    base = "{{DATA_DIR}}/servicebay/agent-kit/checkout"
    for leaf in ("agent-cli", "agent-docs", "assists"):
        assert f"path: {base}/{leaf}" in template_text

    kit = {
        v["name"]: v["hostPath"]
        for v in pod["spec"]["volumes"]
        if v["name"].startswith("agent-kit-")
    }
    assert len(kit) == 3
    for host in kit.values():
        # An older ServiceBay has delivered nothing; `Directory` would fail the
        # whole pod over that, `DirectoryOrCreate` only costs the skills.
        assert host["type"] == "DirectoryOrCreate"
        assert host["path"].startswith("/mnt/data/stacks/servicebay/agent-kit/")


def test_the_readme_says_where_qwen_comes_from(variables):
    """The operator-facing sentence: nothing in the template asks for Qwen, so
    the README has to say who does."""
    readme = (PI_WEB / "README.md").read_text(encoding="utf-8")
    assert "Modell-Kachel" in readme
    assert "Haushaltsmodell" in readme


# ── git credentials for private clones (#1395 slice A) ──────────────────────


GIT_INIT = "pi-web-git-credentials"


@pytest.fixture(scope="module")
def git_init(pod) -> dict:
    return next(c for c in pod["spec"]["initContainers"] if c["name"] == GIT_INIT)


@pytest.fixture(scope="module")
def git_init_script(git_init) -> str:
    assert git_init["args"][:2] == ["sh", "-c"]
    return git_init["args"][2]


def test_the_git_token_is_a_secret_the_operator_types_in(variables):
    """No default and `noAutoGenerate`: a generated random value would write a
    credential file that fails every clone with a 401 and look configured."""
    token = variables["PI_WEB_GIT_TOKEN"]
    assert token["type"] == "secret"
    assert token["noAutoGenerate"] is True
    assert "default" not in token


def test_the_git_user_and_host_are_plain_text_with_conventional_defaults(variables):
    assert variables["PI_WEB_GIT_USER"]["type"] == "text"
    assert variables["PI_WEB_GIT_USER"]["default"] == "x-access-token"
    assert variables["PI_WEB_GIT_HOST"]["type"] == "text"
    assert variables["PI_WEB_GIT_HOST"]["default"] == "github.com"


def test_only_the_credential_init_ever_sees_the_token(pod):
    """The point of the whole arrangement: a session's shell runs in `sessiond`
    and `web`, so `env` there must not name the token at all — it can use the
    credential through git and cannot read it back out of its environment."""
    carriers = [
        c["name"]
        for c in pod["spec"]["initContainers"] + pod["spec"]["containers"]
        if any(e["name"] == "PI_WEB_GIT_TOKEN" for e in c.get("env", []))
    ]
    assert carriers == [GIT_INIT]


def test_the_credential_init_runs_after_the_perms_init(pod):
    """`chmod -R a+rwX /data` would reopen a 0600 store file to the world on
    every start; order is what keeps the mode."""
    order = [c["name"] for c in pod["spec"]["initContainers"]]
    assert order.index("pi-web-data-perms") < order.index(GIT_INIT)


def test_the_credential_init_runs_as_the_image_user(git_init):
    """No `runAsUser: 0` override — the store file must be owned by the UID the
    sessions run as, or a 0600 file is one they cannot read."""
    assert "securityContext" not in git_init
    mounts = {m["mountPath"] for m in git_init["volumeMounts"]}
    assert mounts == {"/data"}


def test_the_store_file_is_written_private(git_init_script):
    assert "umask 077" in git_init_script
    assert "chmod 600" in git_init_script
    assert "store=/data/pi-web/git-credentials" in git_init_script


def test_git_is_pointed_at_the_store_and_trusts_the_workspace(git_init_script):
    """Without the helper every clone prompts; without `safe.directory` git
    refuses a checkout whose files another UID owns."""
    assert 'credential.helper "store --file=$store"' in git_init_script
    assert "--add safe.directory /workspace" in git_init_script
    assert "--add safe.directory '*'" in git_init_script
    assert "--unset-all safe.directory" in git_init_script


def test_the_token_never_reaches_argv_a_url_or_a_log(template_text, git_init_script):
    """`sh -c` carries the variable's *name*; the value goes through the
    environment into a `printf` redirect and nothing echoes it."""
    assert "$PI_WEB_GIT_TOKEN" in git_init_script
    for banned in ("echo", "set -x", "git clone", "--verbose"):
        assert banned not in git_init_script, banned
    assert "{{PI_WEB_GIT_TOKEN}}@" not in template_text
    assert git_init_script.count("PI_WEB_GIT_TOKEN") == 3  # :- guard, printf, unset
    assert "unset PI_WEB_GIT_TOKEN" in git_init_script


def test_a_blank_token_removes_a_stored_one(git_init_script):
    """ServiceBay prunes an env entry that renders empty, so clearing the
    secret in the wizard has to take the credential off the box too."""
    assert 'rm -f "$store"' in git_init_script


def test_the_script_defaults_match_the_declared_ones(git_init_script, variables):
    """The shell fallbacks exist because a cleared text variable is pruned like
    an empty secret; drifting from variables.json would authenticate as
    somebody else's convention."""
    assert (
        "user=${PI_WEB_GIT_USER:-%s}" % variables["PI_WEB_GIT_USER"]["default"]
        in git_init_script
    )
    assert (
        "host=${PI_WEB_GIT_HOST:-%s}" % variables["PI_WEB_GIT_HOST"]["default"]
        in git_init_script
    )


def test_no_token_literal_is_committed_anywhere():
    """A pasted real token in a template, a README or the Dockerfile would ship
    to GHCR and to every box that installs this."""
    prefixes = ("github_pat_", "ghp_", "gho_", "ghs_", "ghu_", "glpat-")
    files = list(PI_WEB.glob("*")) + [ROOT / "pi-web" / "Dockerfile"]
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for prefix in prefixes:
            assert prefix not in text, f"{path.name}: {prefix}"


def test_the_image_ships_git_for_the_credential_helper():
    """`git credential-store` is part of the git package; no git, no helper —
    and the init container's `git config` calls would fail the pod's start."""
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    assert " git " in dockerfile


def test_the_readme_explains_which_github_token_to_create():
    readme = (PI_WEB / "README.md").read_text(encoding="utf-8")
    assert "fine-grained" in readme
    assert "Contents" in readme
    assert "PI_WEB_GIT_TOKEN" in readme


# ── one ServiceBay token per project (#1395 slice C) ────────────────────────


SB_INIT = "pi-web-sb-token"


@pytest.fixture(scope="module")
def sb_init(pod) -> dict:
    return next(c for c in pod["spec"]["initContainers"] if c["name"] == SB_INIT)


@pytest.fixture(scope="module")
def sb_init_script(sb_init) -> str:
    assert sb_init["args"][:2] == ["sh", "-c"]
    return sb_init["args"][2]


def test_servicebay_mints_the_parent_token_itself(variables):
    """`mintApiToken` is the platform's own flag for a variable whose value is a
    ServiceBay token: it mints a read-scoped, never-expiring one at install and
    reuses it on re-install. `noAutoGenerate` would leave an operator step for
    a credential ServiceBay is the issuer of."""
    token = variables["PI_WEB_SB_TOKEN"]
    assert token["type"] == "secret"
    assert token["mintApiToken"] is True
    assert "noAutoGenerate" not in token
    assert "default" not in token


def test_the_parent_is_not_the_per_deploy_read_token(variables):
    """Children die with their parent (servicebay#2049) and ServiceBay revokes
    and re-mints `SB_READ_TOKEN` on every deploy — using it here would silently
    take every project's token away on each redeploy."""
    assert "SB_READ_TOKEN" not in variables
    assert "SB_READ_TOKEN" not in variables["PI_WEB_SB_TOKEN"]["description"].replace(
        "Nicht dasselbe wie `SB_READ_TOKEN`", ""
    )


def test_servicebay_is_addressed_the_way_an_isolated_pod_must(variables, template_text):
    """`127.0.0.1` is this pod itself, and the `servicebay.<domain>` route is
    Authelia-gated — either one answers nothing and looks like a token problem."""
    assert (
        variables["SERVICEBAY_API_URL"]["default"]
        == "http://host.containers.internal:5888"
    )
    assert "127.0.0.1:5888" not in template_text
    assert "https://servicebay" not in template_text


def test_only_the_sb_token_init_ever_sees_the_parent_token(pod):
    """A session's shell runs in `sessiond`; the parent can mint children, so it
    must not be readable out of the session's own environment."""
    carriers = [
        c["name"]
        for c in pod["spec"]["initContainers"] + pod["spec"]["containers"]
        if any(e["name"] == "PI_WEB_SB_TOKEN" for e in c.get("env", []))
    ]
    assert carriers == [SB_INIT]


def test_the_sessions_are_told_where_servicebay_is(pod):
    """The URL is not a credential, and `pi-web-project` inherits it from
    sessiond — without it every session would fall back to a compiled default."""
    sessiond = next(c for c in pod["spec"]["containers"] if c["name"] == "sessiond")
    assert {
        "name": "SERVICEBAY_API_URL",
        "value": "http://host.containers.internal:5888",
    } in sessiond["env"]


def test_the_sb_token_init_runs_after_the_perms_init_as_the_image_user(pod, sb_init):
    order = [c["name"] for c in pod["spec"]["initContainers"]]
    assert order.index("pi-web-data-perms") < order.index(SB_INIT)
    assert "securityContext" not in sb_init
    assert {m["mountPath"] for m in sb_init["volumeMounts"]} == {"/data"}


def test_the_parent_token_file_is_written_private(sb_init_script):
    assert "umask 077" in sb_init_script
    assert 'chmod 600 "$file"' in sb_init_script
    assert "file=$dir/parent-token" in sb_init_script


def test_the_project_entries_get_their_mode_back_on_every_start(sb_init_script):
    """Each entry and its bare token file hold that project's own token and are
    written at runtime; the perms init's recursive `a+rwX` reopens them all on
    the next start. Every file under `projects/`, not the `.json` alone — the
    CLI reads a bare token file beside it (#1398)."""
    assert '"$dir/projects" -type f -exec chmod 600' in sb_init_script


def test_the_parent_token_never_reaches_argv_or_a_log(sb_init_script, template_text):
    assert "$PI_WEB_SB_TOKEN" in sb_init_script
    for banned in ("echo", "set -x", "curl"):
        assert banned not in sb_init_script, banned
    assert "unset PI_WEB_SB_TOKEN" in sb_init_script
    assert sb_init_script.count("PI_WEB_SB_TOKEN") == 3  # :- guard, printf, unset


def test_a_blank_parent_token_removes_a_stored_one(sb_init_script):
    assert 'rm -f "$file"' in sb_init_script


def test_the_image_ships_the_project_cli_and_a_python_to_run_it():
    """The CLI is how a session reads the box at all: Pi ships no MCP, so there
    is no config file a token could live in instead."""
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "COPY --chmod=0755 pi_web_project.py /usr/local/bin/pi-web-project"
        in dockerfile
    )
    assert " python3 " in dockerfile
    assert (ROOT / "pi-web" / "pi_web_project.py").is_file()


def test_the_readme_says_how_a_project_gets_its_token():
    readme = (PI_WEB / "README.md").read_text(encoding="utf-8")
    assert "pi-web-project add" in readme
    assert "PI_WEB_SB_TOKEN" in readme
    # The rule an operator has to know: nothing is adopted by being there.
    assert "pi-web-project remove" in readme
