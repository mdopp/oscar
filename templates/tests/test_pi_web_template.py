"""The `pi-web` template (#1357): where it may reach, and how it holds Qwen.

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
* **The lease state machine.** `202 preparing` and `409 held` are the two
  answers the contract (mdopp/foundry-chronicle#321) spends its words on, and
  both look like "no lease" if they are mishandled — PI WEB would keep working,
  on Gemma, and nobody would notice until an answer was slow.
"""

from __future__ import annotations

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


# ── the lease state machine (foundry-chronicle#321) ─────────────────────────


def test_acquire_payload_is_the_whole_contract(pd):
    """The engine refuses an unknown field, so an extra key is a 400 on every
    single acquire — and PI WEB would silently stay on the household model."""
    assert pd.acquire_payload(14400) == {
        "model": "coding",
        "ttl_s": 14400,
        "holder": "pi-web",
    }


def test_ttl_is_clamped_to_the_window_the_engine_grants(pd):
    assert pd.clamp_ttl("14400") == 14400
    assert pd.clamp_ttl("99999") == 14400
    assert pd.clamp_ttl("60") == 300
    assert pd.clamp_ttl("nonsense") == 14400


def test_renew_leaves_two_more_chances(pd):
    assert pd.renew_after(14400) == 4800
    assert pd.renew_after(300) == 100
    assert pd.renew_after(60) == 60


def test_start_closes_a_window_left_open_by_an_earlier_pi_web(pd):
    """#1361's consumer-side rule: a killed process leaves its window standing
    and nothing in the new one remembers it — the holder is what makes it
    recognisable as ours."""
    for state in ("ready", "preparing"):
        assert pd.is_own_stale_window(200, {"state": state, "holder": "pi-web"})


def test_start_never_closes_a_window_that_is_not_ours(pd):
    """Closing a stranger's window would take the card off a running job —
    exactly what naming a holder exists to prevent."""
    assert not pd.is_own_stale_window(200, {"state": "ready", "holder": "foundry"})
    assert not pd.is_own_stale_window(200, {"state": "ready", "holder": ""})
    # A clean start finds nothing, and an engine that is not answering yet is
    # not evidence of a window either.
    assert not pd.is_own_stale_window(200, {"state": "none", "holder": ""})
    assert not pd.is_own_stale_window(0, {})
    assert not pd.is_own_stale_window(503, {"state": "ready", "holder": "pi-web"})


def test_ready_answer_renews_on_the_engines_own_interval(pd):
    action, delay = pd.next_step(
        200, {"state": "ready", "holder": "pi-web", "renew_after": 4800}, 14400
    )
    assert (action, delay) == ("ready", 4800)


def test_ready_without_a_renew_hint_falls_back_to_a_third(pd):
    """A GET reports the state but no renew_after — the arithmetic is ours."""
    action, delay = pd.next_step(200, {"state": "ready", "holder": "pi-web"}, 14400)
    assert (action, delay) == ("ready", 4800)


def test_preparing_is_polled_not_failed(pd):
    """202 is the first answer of every swap; treating it as an error would
    give up exactly when the lease is being granted."""
    action, delay = pd.next_step(
        202, {"state": "preparing", "holder": "pi-web", "retry_after": 30}, 14400
    )
    assert (action, delay) == ("poll", 30)


def test_held_by_someone_else_is_not_an_error(pd):
    action, delay = pd.next_step(409, {"holder": "foundry"}, 14400)
    assert action == "held"
    assert delay == pd.HELD_RETRY_SECONDS


def test_a_window_that_became_someone_elses_is_not_claimed_as_ours(pd):
    """A GET can report a ready lease held by another service; renewing that
    one would be taking a window we were never granted."""
    action, _ = pd.next_step(200, {"state": "ready", "holder": "foundry"}, 14400)
    assert action == "held"


def test_an_unreachable_engine_retries_instead_of_dying(pd):
    """PI WEB stays usable on whatever model is loaded — an engine restart is
    not a reason to take the UI down with it."""
    action, delay = pd.next_step(0, {}, 14400)
    assert (action, delay) == ("retry", pd.UNREACHABLE_RETRY_SECONDS)
    assert pd.next_step(200, {"state": "none", "holder": ""}, 14400)[0] == "retry"


# ── the host-side unit that owns the window ─────────────────────────────────


def test_lease_unit_lives_and_dies_with_the_pod(pd):
    unit = pd.render_lease_unit(
        "/mnt/data/stacks/pi-web/pi-web-lease.py", "8787", 14400, "/mnt/data/stacks"
    )
    assert "BindsTo=pi-web.service" in unit
    assert "WantedBy=pi-web.service" in unit


def test_lease_unit_releases_on_the_way_down(pd):
    """Without ExecStopPost a stopped PI WEB leaves Qwen loaded until the TTL
    runs out — four hours of a slower household assistant."""
    unit = pd.render_lease_unit(
        "/mnt/data/stacks/pi-web/pi-web-lease.py", "8787", 14400, "/mnt/data/stacks"
    )
    assert "pi-web-lease.py release" in unit
    assert "ExecStopPost=" in unit


def test_lease_unit_carries_the_config_the_script_reads(pd):
    unit = pd.render_lease_unit(
        "/mnt/data/stacks/pi-web/pi-web-lease.py", "8787", 14400, "/mnt/data/stacks"
    )
    assert "Environment=CHAT_PORT=8787" in unit
    assert "Environment=PI_WEB_LEASE_TTL_SECONDS=14400" in unit
    # The two hooks are the only witnesses of a PI WEB start/stop that outlive
    # a redeploy; without DATA_DIR they cannot write the run-state log (#1373).
    assert "Environment=DATA_DIR=/mnt/data/stacks" in unit


def test_the_lease_endpoint_is_the_engine_on_loopback(pd):
    """It has no token: reachability is the authorisation, which is the whole
    reason this runs on the host and not in the pod."""
    assert pd.lease_url("8787") == "http://127.0.0.1:8787/api/model-lease"


def test_the_script_the_unit_runs_is_this_script(pd, tmp_path):
    installed = pd.install_lease_script(str(tmp_path))
    assert installed.endswith("pi-web/pi-web-lease.py")
    assert pathlib.Path(installed).read_text(encoding="utf-8") == (
        PI_WEB / "post-deploy.py"
    ).read_text(encoding="utf-8")


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


# ── not started unless somebody asked (#1373) ───────────────────────────────


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


def test_boot_autostart_is_stripped_from_the_platforms_kube_unit(pd):
    """ServiceBay hard-codes `[Install] WantedBy=default.target` into every
    `.kube` it renders, so Quadlet links pi-web into `default.target.wants` and
    a reboot starts it — taking the coding lease with it. Nothing about that
    looks broken until the household assistant is slow for four hours."""
    stripped = pd.strip_boot_install(PLATFORM_KUBE)
    assert "[Install]" not in stripped
    assert "WantedBy=default.target" not in stripped


def test_stripping_the_install_section_leaves_the_rest_of_the_unit_alone(pd):
    """The `[Kube]` body and the platform's own `[Service]`/`[Unit]`
    directives are ServiceBay's; dropping one of them would change how the pod
    starts, not whether it starts at boot."""
    stripped = pd.strip_boot_install(PLATFORM_KUBE)
    assert "Yaml=pi-web.yml" in stripped
    assert "AutoUpdate=registry" in stripped
    assert "TimeoutStartSec=600" in stripped
    assert "StartLimitIntervalSec=0" in stripped
    # Idempotent: the next deploy re-adds the section, every other run is a
    # no-op that must not rewrite the file (and restart the generator).
    assert pd.strip_boot_install(stripped) == stripped


def test_the_start_on_boot_variable_defaults_to_off(variables):
    spec = variables["PI_WEB_START_ON_BOOT"]
    assert spec["default"] == "false"
    assert spec["options"] == ["true", "false"]


def test_the_state_restored_is_the_one_from_before_the_deploy(pd):
    """A redeploy of a RUNNING PI WEB writes stop+start of its own after the
    `.kube` file; the last entry before it is the operator's."""
    log = "100 running\n200 stopped\n210 running\n"
    assert pd.state_before(log, 150) == "running"


def test_a_pi_web_the_operator_had_stopped_stays_stopped(pd):
    """The deploy starts the service before the post-deploy runs, so its own
    `running` entry is exactly what must not be mistaken for consent."""
    log = "100 running\n120 stopped\n210 running\n"
    assert pd.state_before(log, 150) == "stopped"


def test_no_record_at_all_means_stopped(pd):
    """A first install, or a box that has not had PI WEB up since this landed:
    nobody asked for a coding session, so nobody gets Qwen loaded."""
    assert pd.state_before("", 150) == ""
    assert pd.state_before("garbage\n\nnot-a-timestamp running\n", 150) == ""


def test_the_run_state_log_is_appended_and_bounded(pd, tmp_path):
    for _ in range(pd.RUN_STATE_KEEP + 5):
        pd.record_run_state(str(tmp_path), "running")
    lines = (tmp_path / "pi-web" / pd.RUN_STATE_LOG).read_text().splitlines()
    assert len(lines) == pd.RUN_STATE_KEEP
    assert all(line.split()[1] == "running" for line in lines)


def test_recorded_states_read_back_as_the_state_before_now(pd, tmp_path):
    pd.record_run_state(str(tmp_path), "stopped")
    assert pd.run_state_before_deploy(str(tmp_path), 1e12) == "stopped"
    assert pd.run_state_before_deploy(str(tmp_path), 0) == ""


def test_the_lease_unit_is_enabled_without_now(pd):
    """`--now` starts the lease unit, and `BindsTo=pi-web.service` implies
    `Requires=` — so systemd pulls PI WEB up with it and the deploy takes the
    coding lease on a box where nobody opened a session (#1373)."""
    source = (PI_WEB / "post-deploy.py").read_text(encoding="utf-8")
    assert '"enable", "--now"' not in source


def test_keep_off_at_boot_rewrites_the_unit_and_reloads_the_generator(
    pd, tmp_path, monkeypatch
):
    quadlet = tmp_path / ".config" / "containers" / "systemd"
    quadlet.mkdir(parents=True)
    (quadlet / "pi-web.kube").write_text(PLATFORM_KUBE, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[list[str]] = []
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    assert pd.keep_off_at_boot()
    assert "[Install]" not in (quadlet / "pi-web.kube").read_text(encoding="utf-8")
    # Without the reload the generator keeps yesterday's unit — and the
    # default.target.wants link with it.
    assert ["systemctl", "--user", "daemon-reload"] in calls

    calls.clear()
    assert pd.keep_off_at_boot()
    assert calls == []


def test_restore_stops_a_pi_web_that_was_not_running_before(pd, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(pd, "wait_until_settled", lambda: None)
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    pd.restore_run_state("")
    assert calls == [["systemctl", "--user", "stop", "pi-web.service"]]


def test_restore_waits_for_the_pod_to_settle_before_stopping(pd, monkeypatch):
    """A stop issued while ServiceBay's own deploy-time start (`podman kube
    play`, `Type=notify`) is still running kills that process instead of
    queuing behind it, landing the unit `failed` rather than `inactive`
    (box-verified against #1377). The wait must happen before the stop."""
    order: list[str] = []
    monkeypatch.setattr(pd, "wait_until_settled", lambda: order.append("waited"))
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: order.append("stopped"))
    pd.restore_run_state("")
    assert order == ["waited", "stopped"]


def test_restore_leaves_a_pi_web_that_was_running_alone(pd, monkeypatch):
    """Stopping it would kill the agent sessions the operator had open — the
    whole reason the run state is recorded rather than assumed."""
    calls: list[list[str]] = []
    monkeypatch.setattr(pd, "wait_until_settled", lambda: calls.append("waited"))
    monkeypatch.setattr(pd.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    pd.restore_run_state("running")
    assert calls == []


def test_wait_until_settled_returns_immediately_when_not_activating(pd, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        pd.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"stdout": "inactive"})(),
    )
    slept: list[float] = []
    monkeypatch.setattr(pd.time, "sleep", lambda s: slept.append(s))
    pd.wait_until_settled()
    assert len(calls) == 1
    assert slept == []


def test_wait_until_settled_polls_while_activating_then_returns(pd, monkeypatch):
    states = iter(["activating", "activating", "active"])
    monkeypatch.setattr(
        pd.subprocess,
        "run",
        lambda cmd, **kw: type("R", (), {"stdout": next(states)})(),
    )
    slept: list[float] = []
    monkeypatch.setattr(pd.time, "sleep", lambda s: slept.append(s))
    pd.wait_until_settled()
    assert slept == [pd.SETTLE_POLL_SECONDS, pd.SETTLE_POLL_SECONDS]


def test_wait_until_settled_gives_up_at_the_deadline_rather_than_hanging(
    pd, monkeypatch
):
    """A pod that never leaves `activating` (a genuinely stuck deploy) must
    not wedge post-deploy forever."""
    clock = [0.0]
    monkeypatch.setattr(pd.time, "time", lambda: clock[0])
    monkeypatch.setattr(pd.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(
        pd.subprocess,
        "run",
        lambda cmd, **kw: type("R", (), {"stdout": "activating"})(),
    )
    pd.wait_until_settled()
    assert clock[0] >= pd.SETTLE_DEADLINE_SECONDS
