#!/usr/bin/env python3
"""
post-deploy hook for the `pi-web` template.

Three responsibilities:

  1. **Point the Pi agent at llama-server.** PI WEB has no LLM configuration of
     its own — the model runtime is the Pi Coding Agent's, and a self-hosted
     OpenAI-compatible endpoint is declared in the agent directory's
     `models.json`. This writes that file into the volume the pod mounts, with
     `baseUrl` = `http://host.containers.internal:<LLAMA_PORT>/v1`. Not
     `127.0.0.1` (this pod has its own network namespace), not the LAN address
     (rootless podman refuses it), and never the `llama.<domain>` route, which
     is Authelia-gated and exists for a human with a browser.

  2. **Hold the coding lease for as long as PI WEB runs.** The box serves one
     model at a time; the household model is Gemma 4 E4B and stays that way.
     Qwen 3.8 27B is what the *coding* lease loads, and the lease is an
     endpoint of the Solaris Engine — `POST /api/model-lease {"model":
     "coding", "ttl_s": ..., "holder": "pi-web"}`, contract
     mdopp/foundry-chronicle#321, holder semantics from solarisbay#1347.

     That endpoint carries no token: it is loopback-only and reachability *is*
     the authorisation, so the engine binds `127.0.0.1` alone. An isolated pod
     therefore cannot call it at all — `host.containers.internal` maps to the
     host's LAN address, where nothing is listening. Rather than widen the
     engine's bind (which would expose far more than a lease), the lease is
     held **on the host**: this script copies itself to a durable path and
     installs a systemd user unit bound to `pi-web.service`, which acquires on
     start, renews at a third of the TTL, and releases on stop. Same self-copy
     as the llama template's `gpu-lease.py` (#1320): one file, so the unit and
     the code it runs cannot drift apart.

     A kill leaves that stop out, so the unit follows the consumer-side rule of
     solarisbay#1361: **on start it asks `GET` first and closes a window that
     is already filed under its own holder** before asking for a new one. The
     box gives the card back on its own a grace of two missed renewals after
     the last POST, which is the net when the whole host went down; this is the
     faster path for the ordinary case where PI WEB simply comes back.

  3. **Keep PI WEB off unless somebody asked for it (#1373).** ServiceBay's
     kube-write path hard-codes `[Install] WantedBy=default.target` into every
     `.kube` unit it renders and offers no way for a template to opt out, so
     Quadlet links pi-web into `default.target.wants` and the box would bring
     PI WEB up on its own after a reboot — and with it the host lease unit,
     which takes the coding lease: Qwen loaded, voice on CPU, Solaris muted for
     up to four hours, without anyone asking. A developer tool the operator
     starts on demand must not do that, so this strips the `[Install]` section
     back out of `~/.config/containers/systemd/pi-web.kube` and reloads the
     generator. `PI_WEB_START_ON_BOOT=true` leaves the platform's section in
     place for an operator who does want it up at boot.

     ServiceBay also *starts* the service on every deploy (`deploy.ts` starts
     or restarts before it runs this script), which is the same lease taken by
     a different route. The pre-deploy run state is not readable from here —
     that start may still be *in flight* when this script runs (`Type=notify`,
     `TimeoutStartSec=600`), so a live `is-active` read here cannot be trusted
     either — so the lease unit records each transition it lives through into
     a small log, and the state restored below is the newest entry written
     *before* this deploy rewrote the `.kube` file. No entry at all means
     nobody has had PI WEB running since this landed: stopped. Restoring
     "stopped" always re-issues the stop rather than checking whether it looks
     needed first — see `restore_run_state()`.

Idempotent: identical `models.json` is left alone, and the unit is rewritten
with the same text and re-enabled.

See lib/registry.ts:getTemplatePostDeployScript for the script protocol.
ServiceBay Mustache-renders this file before executing it, so it carries no
double-brace tags at all — every value comes from `env()`
(templates/tests/test_post_deploy_mustache.py).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The Solaris Engine names its leases in one word each; `coding` is the one
# that loads Qwen. The holder is the identity of this *service* — one
# permanent name for every window it ever takes (#1347), never a session.
LEASE_MODEL = "coding"
LEASE_HOLDER = "pi-web"

LEASE_UNIT = "pi-web-model-lease"
LEASE_SCRIPT = "pi-web-lease.py"
POD_UNIT = "pi-web.service"
SYSTEMD_USER_DIR = "~/.config/systemd/user"
QUADLET_DIR = "~/.config/containers/systemd"
KUBE_UNIT = "pi-web.kube"

# One line per transition the lease unit lived through — `<epoch> <state>`.
# Short because only the entries around the last deploy are ever read.
RUN_STATE_LOG = "run-state.log"
RUN_STATE_KEEP = 20

# The aliases llama-server reports (`--alias`) for the two profiles this box
# runs: the coding lease's Qwen, and the household Gemma that answers before
# and after the window. They are pinned in templates/llama/post-deploy.py —
# constants here rather than variables, because a knob that let them drift
# from that file would only ever produce a model list naming something the
# server does not serve.
CODING_ALIAS = "qwen3.8-27b"
CODING_CONTEXT = 81920
HOUSEHOLD_ALIAS = "gemma-4-e4b"
HOUSEHOLD_CONTEXT = 32768

# llama-server ships no authentication, so there is no key to hold — but Pi
# hides a model whose provider has no auth configured at all, so the provider
# carries a placeholder, exactly as upstream's own Ollama example does.
LLAMA_PLACEHOLDER_KEY = "llama"

PROVIDER_ID = "solaris-llama"

# What a `preparing` answer is polled at, and how long the first swap may take
# before the unit gives up and works on whatever model is loaded. The coding
# profile's weights are already on the box, so this is a reload, not a
# download; the ceiling is generous because a reload waits on the household
# model unloading first.
POLL_SECONDS = 30
POLL_DEADLINE_SECONDS = 900
# A lease somebody else holds is not an error and not something to hammer at:
# PI WEB keeps working on the household model and asks again later.
HELD_RETRY_SECONDS = 300
# An unreachable engine (restarting, or not installed) — same idea, shorter.
UNREACHABLE_RETRY_SECONDS = 60


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val if val else default


def jlog(level: str, tag: str, message: str, **args: object) -> None:
    """Emit a TEMPLATE_LOGGING.md-shaped line on stdout."""
    sys.stdout.write(
        json.dumps(
            {
                "ts": datetime.datetime.now().astimezone().isoformat(),
                "level": level,
                "tag": tag,
                "message": message,
                "args": args,
            }
        )
        + "\n"
    )
    sys.stdout.flush()


def http_request(
    url: str,
    payload: dict[str, object] | None = None,
    method: str = "GET",
    timeout: float = 10.0,
) -> tuple[int, dict]:
    """`(status, decoded body)`. Status 0 means the engine did not answer."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, decode_body(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # pylint: disable=broad-except
            body = b""
        return e.code, decode_body(body)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def decode_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


# ── pure decision logic (unit-tested in templates/tests) ─────────────────────


def lease_url(chat_port: str) -> str:
    return f"http://127.0.0.1:{chat_port}/api/model-lease"


def acquire_payload(ttl: int) -> dict[str, object]:
    """The complete request body. The engine refuses an unknown field, so this
    is the whole contract and adding a key is a change on both sides."""
    return {"model": LEASE_MODEL, "ttl_s": ttl, "holder": LEASE_HOLDER}


def clamp_ttl(raw: str) -> int:
    """The engine clamps to 5 minutes … 4 hours; do the same here so the unit
    renews on the window it will actually be given."""
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        ttl = 14400
    return min(max(ttl, 300), 14400)


def renew_after(ttl: int) -> int:
    """A third of the window, so a missed renewal has two more chances before
    the lease expires — the same arithmetic the engine reports."""
    return max(ttl // 3, 60)


def hinted(value: object, fallback: int) -> int:
    """A wait the engine suggested, or ours when it suggested nothing."""
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else fallback
    )


def is_own_stale_window(status: int, body: dict) -> bool:
    """True when the window standing at start is this service's own leftover.

    A process that was killed rather than stopped never ran `ExecStopPost`, and
    nothing in a fresh process remembers that it still holds the card — that
    state died with the old one. The `holder` from #1347 is what makes "mine"
    distinguishable from "somebody else's", so the unit closes its own and
    never a stranger's. On a clean start there is no window and this is a
    no-op (contract solarisbay#1361).
    """
    return (
        status == 200
        and body.get("state") in ("preparing", "ready")
        and body.get("holder") == LEASE_HOLDER
    )


def next_step(status: int, body: dict, ttl: int) -> tuple[str, int]:
    """`(what to do, seconds to wait)` for one lease answer.

    The same table reads a POST and a GET, which is why the state is taken
    from the body rather than from the status alone: only the POST answers
    202/409, but a GET can report the very same `preparing` or a window that
    has meanwhile become somebody else's.
    """
    if status == 409:
        return "held", HELD_RETRY_SECONDS
    if status not in (200, 202):
        return "retry", UNREACHABLE_RETRY_SECONDS
    state = body.get("state")
    holder = body.get("holder", LEASE_HOLDER)
    if holder not in ("", LEASE_HOLDER):
        return "held", HELD_RETRY_SECONDS
    if state == "ready":
        # A POST names the renewal interval; a GET reports the state without
        # one, so the arithmetic falls back to ours.
        return "ready", hinted(body.get("renew_after"), renew_after(ttl))
    if state == "preparing":
        return "poll", hinted(body.get("retry_after"), POLL_SECONDS)
    # `none` on a GET: the request was never picked up, or the window ended.
    return "retry", UNREACHABLE_RETRY_SECONDS


def models_document(llama_port: str) -> dict:
    """The Pi agent's `models.json`: one OpenAI-compatible provider, the two
    aliases this box's llama-server actually answers to.

    Both are listed on purpose. llama-server runs one model at a time, so the
    coding alias is what answers while the lease is held and the household one
    is what answers otherwise; a list with only one of them would name a model
    that is absent for half the day.
    """
    return {
        "providers": {
            PROVIDER_ID: {
                "baseUrl": f"http://host.containers.internal:{llama_port}/v1",
                "api": "openai-completions",
                "apiKey": LLAMA_PLACEHOLDER_KEY,
                # llama-server takes neither the `developer` role nor
                # `reasoning_effort`, and the coding profile is loaded with
                # thinking off (#1321) — asking for either turns every request
                # into a 400.
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [
                    {
                        "id": CODING_ALIAS,
                        "name": "Qwen 3.8 27B (Coding-Lease)",
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": CODING_CONTEXT,
                        "maxTokens": 16384,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    },
                    {
                        "id": HOUSEHOLD_ALIAS,
                        "name": "Gemma 4 E4B (Haushaltsmodell)",
                        "reasoning": False,
                        "input": ["text", "image"],
                        "contextWindow": HOUSEHOLD_CONTEXT,
                        "maxTokens": 16384,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    },
                ],
            }
        }
    }


def strip_boot_install(kube_text: str) -> str:
    """The `.kube` unit with its `[Install]` section removed.

    That section is ServiceBay's, not the template's: every kube-write path in
    the platform emits `[Install] WantedBy=default.target` literally and takes
    no annotation to say otherwise. Dropping it is what keeps Quadlet from
    linking pi-web into `default.target.wants` — everything else in the file
    (`[Kube]`, the platform's `[Service]`/`[Unit]` directives) is left byte for
    byte as it was.
    """
    out: list[str] = []
    in_install = False
    for line in kube_text.splitlines(keepends=True):
        header = line.strip()
        if header.startswith("[") and header.endswith("]"):
            in_install = header == "[Install]"
        if not in_install:
            out.append(line)
    return "".join(out)


def state_before(log_text: str, cutoff: float) -> str:
    """The newest recorded run state older than `cutoff` — `""` if none.

    `cutoff` is the mtime of the `.kube` file this deploy wrote, which is the
    last moment before ServiceBay started the service. The deploy's own start
    (and the stop half of its restart) are recorded after it and are therefore
    skipped, so what comes back is the state the operator left PI WEB in.
    """
    newest = ""
    for line in log_text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            when = float(parts[0])
        except ValueError:
            continue
        if when < cutoff:
            newest = parts[1]
    return newest


def render_lease_unit(script: str, chat_port: str, ttl: int, data_dir: str) -> str:
    """The host-side unit that owns the window.

    `BindsTo` + `WantedBy` on the pod unit is the whole lifecycle: systemd
    starts this with PI WEB and stops it with PI WEB, and `ExecStopPost` gives
    the card back on the way down — including when the pod is stopped by an
    operator, a redeploy or a reboot.

    `DATA_DIR` is here because those same two hooks are the only witnesses of
    a PI WEB start or stop that survive a redeploy: they write the run-state
    log the next post-deploy restores from (#1373).
    """
    return (
        "[Unit]\n"
        "Description=Hold the Solaris coding lease while PI WEB runs (#1357)\n"
        f"After={POD_UNIT}\n"
        f"BindsTo={POD_UNIT}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=CHAT_PORT={chat_port}\n"
        f"Environment=DATA_DIR={data_dir}\n"
        f"Environment=PI_WEB_LEASE_TTL_SECONDS={ttl}\n"
        f"ExecStart={sys.executable} {script} hold\n"
        f"ExecStopPost={sys.executable} {script} release\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        f"WantedBy={POD_UNIT}\n"
    )


# ── the box side ────────────────────────────────────────────────────────────


def agent_dir(data_dir: str) -> str:
    """The Pi agent directory as the containers see it at /data/pi-agent."""
    return os.path.join(data_dir, "pi-web", "data", "pi-agent")


def write_models_json(data_dir: str, llama_port: str) -> bool:
    path = os.path.join(agent_dir(data_dir), "models.json")
    text = json.dumps(models_document(llama_port), indent=2) + "\n"
    try:
        if os.path.exists(path) and open(path, encoding="utf-8").read() == text:
            jlog("info", "pi-web:models", "models.json already current", path=path)
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        jlog(
            "error",
            "pi-web:models",
            "could not write models.json; PI WEB will start with no model to pick",
            path=path,
            error=str(e),
        )
        return False
    jlog(
        "info",
        "pi-web:models",
        "models.json written",
        path=path,
        provider=PROVIDER_ID,
        models=[CODING_ALIAS, HOUSEHOLD_ALIAS],
    )
    return True


def install_lease_script(data_dir: str) -> str:
    """Copy this script to a durable path the systemd unit can call."""
    dst = os.path.join(data_dir, "pi-web", LEASE_SCRIPT)
    try:
        with open(os.path.realpath(__file__), encoding="utf-8") as f:
            self_src = f.read()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(self_src)
        os.chmod(dst, 0o755)
    except OSError as e:
        jlog(
            "warn",
            "pi-web:lease",
            "could not install the lease script; PI WEB will run on the household model",
            path=dst,
            error=str(e),
        )
        return ""
    jlog("info", "pi-web:lease", "lease script installed", path=dst)
    return dst


def install_lease_unit(script: str, chat_port: str, ttl: int, data_dir: str) -> bool:
    if not script:
        return False
    unit_dir = os.path.expanduser(SYSTEMD_USER_DIR)
    name = f"{LEASE_UNIT}.service"
    try:
        os.makedirs(unit_dir, exist_ok=True)
        with open(os.path.join(unit_dir, name), "w", encoding="utf-8") as f:
            f.write(render_lease_unit(script, chat_port, ttl, data_dir))
        os.chmod(os.path.join(unit_dir, name), 0o644)
    except OSError as e:
        jlog(
            "error",
            "pi-web:lease",
            "could not install the lease unit; PI WEB will run on the household model",
            path=unit_dir,
            error=str(e),
        )
        return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    # `enable`, never `enable --now`: this unit is `BindsTo=pi-web.service`,
    # which implies `Requires=`, so starting it pulls PI WEB up with it — the
    # deploy would take the coding lease on a box where nobody asked for PI WEB
    # at all (#1373). The `WantedBy=pi-web.service` link is what starts it, and
    # a PI WEB that is already up gets it started explicitly below.
    subprocess.run(
        ["systemctl", "--user", "enable", name], check=False, capture_output=True
    )
    if pod_is_active():
        subprocess.run(
            ["systemctl", "--user", "start", f"{LEASE_UNIT}.service"],
            check=False,
            capture_output=True,
        )
    jlog("info", "pi-web:lease", "lease unit installed", unit=name)
    return True


# ── boot behaviour and the run state (#1373) ────────────────────────────────


def kube_unit_path() -> str:
    return os.path.join(os.path.expanduser(QUADLET_DIR), KUBE_UNIT)


def pod_is_active() -> bool:
    out = subprocess.run(
        ["systemctl", "--user", "is-active", POD_UNIT],
        check=False,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() == "active"


def run_state_path(data_dir: str) -> str:
    return os.path.join(data_dir, "pi-web", RUN_STATE_LOG)


def record_run_state(data_dir: str, state: str) -> None:
    path = run_state_path(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        kept = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                kept = f.read().splitlines()[-(RUN_STATE_KEEP - 1) :]
        kept.append(f"{int(time.time())} {state}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
    except OSError as e:
        jlog("warn", "pi-web:runstate", "could not record run state", error=str(e))


def run_state_before_deploy(data_dir: str, cutoff: float) -> str:
    try:
        with open(run_state_path(data_dir), encoding="utf-8") as f:
            return state_before(f.read(), cutoff)
    except OSError:
        return ""


def keep_off_at_boot() -> bool:
    """Take ServiceBay's `[Install] WantedBy=default.target` back out of the
    `.kube` unit and reload the generator. No-op once it is gone."""
    path = kube_unit_path()
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError as e:
        jlog(
            "warn",
            "pi-web:boot",
            "could not read the kube unit; PI WEB may still start at boot",
            path=path,
            error=str(e),
        )
        return False
    stripped = strip_boot_install(current)
    if stripped == current:
        jlog("info", "pi-web:boot", "kube unit already has no [Install]", path=path)
        return True
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(stripped)
    except OSError as e:
        jlog(
            "error",
            "pi-web:boot",
            "could not rewrite the kube unit; PI WEB would start at boot and take the coding lease",
            path=path,
            error=str(e),
        )
        return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    jlog("info", "pi-web:boot", "pi-web unlinked from default.target", path=path)
    return True


def restore_run_state(desired: str) -> None:
    """Put PI WEB back in the state it was in before ServiceBay's deploy
    started it. Anything but a recorded `running` means stop.

    Always issues the stop rather than gating it on `pod_is_active()` first:
    ServiceBay's own start (`Type=notify`, `TimeoutStartSec=600`) can still be
    mid-flight when this runs, so an is-active read here can land before
    systemd has settled, read `False`, and skip the stop — leaving PI WEB (and
    the coding lease it pulls) running, box-verified against #1375. A `stop`
    on a unit that is already inactive, or one that is still starting, is a
    safe no-op / queued job either way.
    """
    if desired == "running":
        jlog("info", "pi-web:runstate", "PI WEB was running before the deploy; left up")
        return
    subprocess.run(
        ["systemctl", "--user", "stop", POD_UNIT], check=False, capture_output=True
    )
    jlog(
        "info",
        "pi-web:runstate",
        "PI WEB was not running before the deploy; stopped again so the coding lease stays free",
        recorded=desired or "none",
    )


def hold(chat_port: str, ttl: int) -> int:
    """Clean up after a previous run, acquire, wait out a swap, then renew for
    as long as PI WEB runs."""
    url = lease_url(chat_port)
    status, body = http_request(url, None, "GET")
    if is_own_stale_window(status, body):
        jlog(
            "info",
            "pi-web:lease",
            "a window from an earlier PI WEB is still open; closing it before asking again",
            state=body.get("state", ""),
        )
        release(chat_port)
    while True:
        status, body = http_request(url, acquire_payload(ttl), "POST")
        action, delay = next_step(status, body, ttl)
        waited = 0
        while action == "poll" and waited < POLL_DEADLINE_SECONDS:
            time.sleep(delay)
            waited += delay
            status, body = http_request(url, None, "GET")
            action, delay = next_step(status, body, ttl)
        if action == "ready":
            jlog(
                "info",
                "pi-web:lease",
                "coding lease held",
                alias=body.get("alias", CODING_ALIAS),
                renew_in_s=delay,
            )
        elif action == "held":
            jlog(
                "info",
                "pi-web:lease",
                "the card is leased to someone else; PI WEB works on the loaded model",
                holder=body.get("holder", ""),
                retry_in_s=delay,
            )
        else:
            jlog(
                "warn",
                "pi-web:lease",
                "no coding lease; PI WEB works on the loaded model",
                status=status,
                retry_in_s=delay,
            )
        time.sleep(delay)


def release(chat_port: str) -> int:
    status, body = http_request(
        lease_url(chat_port), {"holder": LEASE_HOLDER}, "DELETE"
    )
    if status == 409:
        # Somebody else's window — leave it alone; that is what naming a holder
        # is for.
        jlog(
            "info",
            "pi-web:lease",
            "lease belongs to another holder; nothing released",
            holder=body.get("holder", ""),
        )
        return 0
    jlog("info", "pi-web:lease", "coding lease released", status=status)
    return 0


def main() -> int:
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    chat_port = env("CHAT_PORT", "8787")
    ttl = clamp_ttl(env("PI_WEB_LEASE_TTL_SECONDS", "14400"))

    if len(sys.argv) > 1 and sys.argv[1] == "hold":
        # Recorded here rather than inside hold(), which calls release() itself
        # to close a stale window — that is not PI WEB going down.
        record_run_state(data_dir, "running")
        return hold(chat_port, ttl)
    if len(sys.argv) > 1 and sys.argv[1] == "release":
        record_run_state(data_dir, "stopped")
        return release(chat_port)

    start_on_boot = env("PI_WEB_START_ON_BOOT", "false") == "true"
    # Read before keep_off_at_boot() rewrites the file: the mtime ServiceBay's
    # own write left is the dividing line between the operator's transitions
    # and the deploy's.
    try:
        deployed_at = os.path.getmtime(kube_unit_path())
    except OSError:
        deployed_at = time.time()

    write_models_json(data_dir, env("LLAMA_PORT", "11435"))
    if start_on_boot:
        jlog(
            "info",
            "pi-web:boot",
            "PI_WEB_START_ON_BOOT is true; leaving the platform's autostart in place",
        )
    else:
        keep_off_at_boot()
        # Before the lease unit is enabled below, so a PI WEB the deploy is
        # about to stop never gets a lease taken for it on the way past.
        restore_run_state(run_state_before_deploy(data_dir, deployed_at))
    install_lease_unit(install_lease_script(data_dir), chat_port, ttl, data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
