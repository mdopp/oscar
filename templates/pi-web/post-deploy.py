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

     Whatever llama-server currently serves is what PI WEB answers on: the
     coding alias while somebody holds the coding lease, the household alias
     otherwise. PI WEB never asks for the swap itself.

  2. **Retire the host-side lease unit (#1392).** Until now PI WEB took the
     coding lease by simply being started: `pi-web-model-lease.service` was
     `BindsTo=pi-web.service`, so a start — including ServiceBay's own start on
     every deploy, and the box's after every reboot — loaded Qwen, moved voice
     onto the CPU and left the household assistant slow for up to four hours
     that nobody had asked for. The counterweight was to keep PI WEB switched
     off (#1373: strip the platform's `[Install]`, restore the pre-deploy run
     state), which left `pi.<domain>` dead until somebody started the service
     by hand.

     Since #1374/#1381 the lease has an operator-facing route of its own — the
     model tile in Solaris — so PI WEB no longer needs one. It runs around the
     clock like any other service (the platform's `[Install]` stays), and this
     script stops, disables and removes the lease unit and its script copy. A
     window still filed under holder `pi-web` from before the upgrade is closed
     once, here, so the box does not sit on Qwen until the TTL runs out.

Idempotent: identical `models.json` is left alone, and a lease unit that is
already gone (and a lease that is not ours) is a no-op.

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
import urllib.error
import urllib.request

# The Solaris Engine names its leases in one word each; `coding` is the one
# that loads Qwen. `pi-web` is the holder this service used to file its windows
# under (#1347) — kept because the one-time cleanup below has to recognise them.
LEASE_HOLDER = "pi-web"

LEASE_UNIT = "pi-web-model-lease"
LEASE_SCRIPT = "pi-web-lease.py"
POD_UNIT = "pi-web.service"
SYSTEMD_USER_DIR = "~/.config/systemd/user"
QUADLET_DIR = "~/.config/containers/systemd"
KUBE_UNIT = "pi-web.kube"
BOOT_INSTALL = "[Install]\nWantedBy=default.target\n"

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


def is_own_stale_window(status: int, body: dict) -> bool:
    """True when the window standing right now is this service's own leftover.

    The retired lease unit filed its windows under holder `pi-web` (#1347), and
    one of them can still be open when this upgrade lands — the unit is removed
    below, so nothing would ever give that card back and the box would sit on
    Qwen until the TTL ran out. The holder is what makes "ours" distinguishable
    from "somebody else's", including the model tile's (#1374), whose window
    must be left alone. Nothing open, or somebody else's: no-op.
    """
    return (
        status == 200
        and body.get("state") in ("preparing", "ready")
        and body.get("holder") == LEASE_HOLDER
    )


def add_boot_install(kube_text: str) -> str:
    """The `.kube` unit with an `[Install] WantedBy=default.target` section.

    Unchanged when the section is already there — that is the ordinary case, so
    the file is not rewritten and the generator not reloaded on every deploy.
    The one box this actually edits is the one #1373 stripped.
    """
    if any(line.strip() == "[Install]" for line in kube_text.splitlines()):
        return kube_text
    separator = "" if kube_text.endswith("\n\n") else "\n"
    return kube_text + separator + BOOT_INSTALL


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


# ── the box side ────────────────────────────────────────────────────────────


def agent_dir(data_dir: str) -> str:
    """The Pi agent directory as the containers see it at /data/pi-agent."""
    return os.path.join(data_dir, "pi-web", "data", "pi-agent")


def kube_unit_path() -> str:
    return os.path.join(os.path.expanduser(QUADLET_DIR), KUBE_UNIT)


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


# ── retiring the host-side lease unit (#1392) ───────────────────────────────


def retire_lease_unit(data_dir: str) -> None:
    """Stop, disable and remove `pi-web-model-lease.service` and its script.

    Removing the unit file is not enough on its own: it is `BindsTo=` and
    `WantedBy=pi-web.service`, so a copy left enabled would keep being started
    with PI WEB — which now runs around the clock — and take the coding lease
    on every boot. `disable` is what drops the `pi-web.service.wants` link.
    This runs before pi-web is started below, so the start never passes a unit
    that is still linked to it.
    """
    unit = f"{LEASE_UNIT}.service"
    for verb in ("stop", "disable"):
        subprocess.run(
            ["systemctl", "--user", verb, unit], check=False, capture_output=True
        )
    removed = []
    for path in (
        os.path.join(os.path.expanduser(SYSTEMD_USER_DIR), unit),
        os.path.join(data_dir, "pi-web", LEASE_SCRIPT),
    ):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    jlog(
        "info",
        "pi-web:lease",
        "lease unit retired; the coding lease now comes from the Solaris model tile",
        unit=unit,
        removed=removed,
    )


def release_own_lease(chat_port: str) -> None:
    """Give back a window still filed under holder `pi-web`, once."""
    url = lease_url(chat_port)
    status, body = http_request(url, None, "GET")
    if not is_own_stale_window(status, body):
        jlog(
            "info",
            "pi-web:lease",
            "no coding lease of ours to give back",
            state=body.get("state", ""),
            holder=body.get("holder", ""),
        )
        return
    status, body = http_request(url, {"holder": LEASE_HOLDER}, "DELETE")
    jlog(
        "info",
        "pi-web:lease",
        "released the coding lease the retired unit had taken",
        status=status,
    )


def restore_boot_autostart() -> None:
    """Put the `[Install]` section back into the `.kube` unit and reload.

    ServiceBay renders `[Install] WantedBy=default.target` into every `.kube`
    it writes, so a fresh install already has it — but a box upgraded from
    #1373 carries a unit this template *stripped*, and ServiceBay only rewrites
    the file when the rendered spec changed. Adding it back here covers both.

    Not `systemctl enable`: a Quadlet-generated unit cannot be enabled by
    systemctl at all. The `[Install]` section is read by the generator, which
    creates the `default.target.wants` link itself on the reload below.
    """
    path = kube_unit_path()
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError as e:
        jlog(
            "warn",
            "pi-web:boot",
            "could not read the kube unit; PI WEB may not come back after a reboot",
            path=path,
            error=str(e),
        )
        return
    restored = add_boot_install(current)
    if restored == current:
        jlog("info", "pi-web:boot", "kube unit already starts at boot", path=path)
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(restored)
    except OSError as e:
        jlog(
            "error",
            "pi-web:boot",
            "could not rewrite the kube unit; PI WEB will not come back after a reboot",
            path=path,
            error=str(e),
        )
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    jlog("info", "pi-web:boot", "pi-web linked into default.target", path=path)


def start_pod() -> None:
    """Bring PI WEB up. A no-op for an already running pod, and the thing that
    ends the #1373 era on an upgraded box, where the service is left stopped."""
    subprocess.run(
        ["systemctl", "--user", "start", POD_UNIT], check=False, capture_output=True
    )
    jlog("info", "pi-web:boot", "PI WEB started", unit=POD_UNIT)


def main() -> int:
    data_dir = env("DATA_DIR", "/mnt/data/stacks")

    write_models_json(data_dir, env("LLAMA_PORT", "11435"))
    retire_lease_unit(data_dir)
    restore_boot_autostart()
    start_pod()
    release_own_lease(env("CHAT_PORT", "8787"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
