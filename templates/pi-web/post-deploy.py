#!/usr/bin/env python3
"""
post-deploy hook for the `pi-web` template.

Two responsibilities:

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


def render_lease_unit(script: str, chat_port: str, ttl: int) -> str:
    """The host-side unit that owns the window.

    `BindsTo` + `WantedBy` on the pod unit is the whole lifecycle: systemd
    starts this with PI WEB and stops it with PI WEB, and `ExecStopPost` gives
    the card back on the way down — including when the pod is stopped by an
    operator, a redeploy or a reboot.
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


def install_lease_unit(script: str, chat_port: str, ttl: int) -> bool:
    if not script:
        return False
    unit_dir = os.path.expanduser(SYSTEMD_USER_DIR)
    name = f"{LEASE_UNIT}.service"
    try:
        os.makedirs(unit_dir, exist_ok=True)
        with open(os.path.join(unit_dir, name), "w", encoding="utf-8") as f:
            f.write(render_lease_unit(script, chat_port, ttl))
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
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", name],
        check=False,
        capture_output=True,
    )
    jlog("info", "pi-web:lease", "lease unit installed", unit=name)
    return True


def hold(chat_port: str, ttl: int) -> int:
    """Acquire, wait out a swap, then renew for as long as PI WEB runs."""
    url = lease_url(chat_port)
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
        return hold(chat_port, ttl)
    if len(sys.argv) > 1 and sys.argv[1] == "release":
        return release(chat_port)

    write_models_json(data_dir, env("LLAMA_PORT", "11435"))
    install_lease_unit(install_lease_script(data_dir), chat_port, ttl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
