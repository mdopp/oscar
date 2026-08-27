#!/usr/bin/env python3
"""
post-deploy hook for the `ollama` template.

Two responsibilities:

  1. **Pull the default model.** Ollama doesn't pull on first start;
     it serves what's already on disk. The wizard knows which model
     the operator picked, so trigger the pull here once the pod is
     reachable.

  2. **Register an HTTP health check.** The auto-created
     `service:ollama` check catches "systemd thinks ollama is down";
     adding an `http` check against `/api/tags` catches the
     degraded-but-running cases (corrupt model store, GPU OOM, disk
     full) that systemd would still see as `active`.

Idempotent: a second run finds the model already cached and skips
the pull; the health-check API does upsert-by-id.

See lib/registry.ts:getTemplatePostDeployScript for the script
protocol and docs/TEMPLATE_AUTHORING.md § Health checks for the
check-registration contract.
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
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # pylint: disable=broad-except
            body = b""
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def wait_for_ready(ollama_url: str, deadline_sec: int) -> bool:
    """Poll /api/tags until Ollama responds 200."""
    started = time.time()
    last_beat = 0.0
    while time.time() - started < deadline_sec:
        status, _ = http_request(f"{ollama_url}/api/tags", timeout=5)
        if status == 200:
            return True
        elapsed = time.time() - started
        if elapsed - last_beat >= 10:
            jlog(
                "info",
                "ollama:wait",
                "still waiting for Ollama API",
                elapsed_sec=int(elapsed),
            )
            last_beat = elapsed
        time.sleep(3)
    return False


def model_present(ollama_url: str, model: str) -> bool:
    """Return True iff Ollama's /api/tags lists `model` (exact match against
    `name`). Used as a defensive post-pull check (#1047): the `ollama pull`
    CLI is known to exit 0 even when manifest write fails, and the HTTP
    /api/pull streaming endpoint can also report `success` while leaving
    the manifest unwritten if the underlying filesystem perms are wrong
    (e.g. a `library/<namespace>/` dir left root-owned by an earlier
    rootful run, biting the next rootless pull). Always re-check via
    /api/tags before declaring a pull successful."""
    try:
        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ) as e:
        jlog("warn", "ollama:verify", "/api/tags probe failed", error=str(e))
        return False
    for entry in payload.get("models", []) or []:
        if str(entry.get("name") or "") == model:
            return True
    return False


def pull_model(ollama_url: str, model: str, stall_sec: int) -> bool:
    """Trigger a streaming pull and wait for the done line.

    Fails only after `stall_sec` with no download progress — not on a total
    wall-clock budget — so a slow link can take as long as it needs as long
    as bytes keep flowing (#109). Download progress (percent + MB) is logged
    every PROGRESS_LOG_INTERVAL_SEC so the operator sees movement instead of
    a silent multi-GB wait.

    Post-pull verifies via /api/tags (#1047) — neither the CLI nor the
    HTTP streaming endpoint reliably surfaces manifest-write failures.
    A pull that reports `success` but never lands the model in /api/tags
    is treated as a failure here so callers can fall back / fail loud
    instead of leaving the operator with a 404-on-first-chat box."""
    PROGRESS_LOG_INTERVAL_SEC = 15
    body = json.dumps({"name": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    # `stall_sec` doubles as the socket read timeout, so a dead connection
    # (no bytes at all) raises after the same window the in-loop stall check
    # uses for a live-but-stuck stream.
    try:
        with urllib.request.urlopen(req, timeout=stall_sec) as resp:
            last_status = ""
            last_log = 0.0
            last_progress_at = started
            last_seen = ""
            for raw in resp:
                now = time.time()
                try:
                    chunk = json.loads(raw.decode("utf-8").strip())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if chunk.get("error"):
                    jlog(
                        "error",
                        "ollama:pull",
                        "pull error",
                        model=model,
                        error=str(chunk.get("error")),
                    )
                    return False
                status = str(chunk.get("status", ""))
                completed = int(chunk.get("completed") or 0)
                total = int(chunk.get("total") or 0)
                # Any change in status or downloaded bytes counts as progress;
                # only a genuinely stuck pull lets `last_progress_at` go stale.
                fingerprint = f"{status}:{completed}"
                if fingerprint != last_seen:
                    last_seen = fingerprint
                    last_progress_at = now
                elif now - last_progress_at > stall_sec:
                    jlog(
                        "error",
                        "ollama:pull",
                        "model pull stalled — no download progress within the stall window",
                        model=model,
                        stall_sec=stall_sec,
                        last_status=status,
                    )
                    return False
                # Log on status change, else throttle to one progress line per
                # interval so a multi-GB blob shows steady, visible movement.
                if status and (
                    status != last_status or now - last_log >= PROGRESS_LOG_INTERVAL_SEC
                ):
                    if total > 0 and completed > 0:
                        pct = int(completed * 100 / total)
                        done_mb = completed // (1024 * 1024)
                        total_mb = total // (1024 * 1024)
                        # ASCII bar in the message so the log line reads as a
                        # progress bar wherever it surfaces; structured fields
                        # stay in args for a future UI bar (servicebay#1288).
                        filled = pct * 20 // 100
                        bar = "#" * filled + "-" * (20 - filled)
                        jlog(
                            "info",
                            "ollama:pull",
                            f"{model} [{bar}] {pct}% ({done_mb}/{total_mb} MB)",
                            model=model,
                            percent=pct,
                            completed_mb=done_mb,
                            total_mb=total_mb,
                        )
                    else:
                        jlog("info", "ollama:pull", status, model=model)
                    last_status = status
                    last_log = now
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        jlog("error", "ollama:pull", "pull failed", model=model, error=str(e))
        return False
    if not model_present(ollama_url, model):
        jlog(
            "error",
            "ollama:pull",
            "stream reported success but model is not in /api/tags — manifest write likely failed silently (#1047). Check `ls -la /mnt/data/stacks/ollama/models/manifests/registry.ollama.ai/library/` for non-`core:core` ownership on the host.",
            model=model,
        )
        return False
    jlog(
        "info",
        "ollama:pull",
        "model ready",
        model=model,
        elapsed_sec=int(time.time() - started),
    )
    return True


def local_chat_tags(ollama_url: str) -> list[str]:
    """The locally installed chat tags from /api/tags (embed models skipped).
    The install env can't be trusted for the model list — OLLAMA_EXTRA_MODELS
    arrived empty on the box (solarisbay#339) — but what's pulled locally is
    ground truth for what should be warm."""
    req = urllib.request.Request(f"{ollama_url}/api/tags")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    tags = []
    for m in body.get("models") or []:
        name = str(m.get("name") or m.get("model") or "")
        if name and "embed" not in name:
            tags.append(name)
    return tags


def warm_load_order(tags: list[str], fast_model: str) -> list[str]:
    """Order the chat tags for warm-loading: the configured fast model first,
    then the rest alphabetically.

    Order is load-bearing (solarisbay#340, box-measured): with the small fast
    model resident a 12b load co-exists, but loading 12b first makes the
    subsequent fast-model load evict it, so the household hot path ends up
    cold. The fast model is the hot path, so it goes first.

    Never derive this order from the `size` field /api/tags reports
    (solarisbay#1217, box-measured): gemma4:e4b is a per-layer-embedding model
    whose on-disk weights report 9,608,350,718 while gemma4:12b reports
    7,556,508,396 — yet resident `size_vram` is the reverse (~3.3 GB vs
    ~8.4 GB). Disk size is not a proxy for VRAM cost, so identity from config
    is the only signal that stays true.
    """
    ordered = sorted(set(tags))
    want = _canonical_tag(fast_model)
    fast = [t for t in ordered if _canonical_tag(t) == want]
    if not fast:
        jlog(
            "warn",
            "ollama:warm",
            "configured fast model is not installed locally; warming in alphabetical order",
            fast_model=fast_model,
            tags=ordered,
        )
        return ordered
    return fast + [t for t in ordered if t not in fast]


def _canonical_tag(tag: str) -> str:
    """`gemma4` and `gemma4:latest` name the same model to Ollama."""
    tag = tag.strip()
    return tag if ":" in tag else f"{tag}:latest"


# How long OUR warm call asks Ollama to hold the household fast model. The
# service-wide OLLAMA_KEEP_ALIVE default is short on purpose (#1264): it applies
# to every model any consumer on this box loads, so as a blanket 24h it let one
# forgetful neighbour squat the GPU for a day at the household's expense. 24h is
# right for our model (#268) — so we ask for it, per request, for our model only.
FAST_MODEL_KEEP_ALIVE = "24h"


def warm_load_model(ollama_url: str, model: str, timeout_sec: int = 180) -> bool:
    """Load `model` into VRAM with a 1-token generate so the first real turn
    after a deploy is warm. Every (re)deploy restarts the ollama unit and
    drops all residents — without this, the first voice turn lands on a cold
    model and the PE gives up before the answer arrives (box-observed
    2026-06-12: 9-66s intent stage, "blinkt nur blau"). Best-effort.

    The call carries an explicit `keep_alive` so the fast model's long hold
    comes from us and not from the service-wide default (#1264)."""
    body = json.dumps(
        {
            "model": model,
            "prompt": "Hi",
            "stream": False,
            "keep_alive": FAST_MODEL_KEEP_ALIVE,
            "options": {"num_predict": 1},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            ok = 200 <= resp.status < 300
        jlog(
            "info",
            "ollama:warm",
            "model warm-loaded",
            model=model,
            seconds=round(time.time() - t0, 1),
        )
        return ok
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        jlog("warn", "ollama:warm", "warm-load failed", model=model, error=str(e))
        return False


def warm_installed_models(
    ollama_url: str, fast_model: str, fallback: list[str]
) -> None:
    """Warm the configured fast model — and nothing else.

    Warming every installed chat tag was self-cancelling (solarisbay#1258,
    box-measured): the second tag loaded ~45 s after the fast model and
    evicted it (`predicted 8.3 GiB / available 8.0 GiB`, 7× in 14 days), so
    the re-warm left the household hot path colder than it found it. The big
    tag is not unused — foundry-chronicle shares this box and loads it itself
    at `/session start`, in a background thread, minutes to hours before its
    first scene — it just doesn't need OUR pre-warm. Unconditional by
    agreement with that side (mdopp/foundry-chronicle#299): warming "only
    when a lease exists" would couple this script to their lease state for
    56 seconds once an evening.

    Source of truth = the locally installed tags (solarisbay#339: the
    env-derived list arrived empty on the box); `fallback` is only used when
    /api/tags can't be read. Which tag is the fast one comes from
    warm_load_order() — configured fast-model identity first, never a size
    field (solarisbay#1217)."""
    warm_tags = local_chat_tags(ollama_url) or [m for m in fallback if m]
    for warm in warm_load_order(warm_tags, fast_model)[:1]:
        warm_load_model(ollama_url, warm)


# The re-warm unit (#1236). warm_load_model() used to live only in this
# post-deploy, so ANY ollama start that wasn't a template deploy — a podman
# auto-update (the container carries AutoUpdate=registry), a reboot, a manual
# `systemctl restart`, or ServiceBay's GPU-Quadlet force-recreate
# (mdopp/servicebay#2618) — left the household fast model cold indefinitely.
# Box-observed 2026-08-25: cold for 13.5h, then a 2m09s first voice turn that
# Home Assistant gave up on. So the warm is bound to the ollama unit's OWN
# start: this same script, copied to a durable path, re-run in warm-only mode
# (OLLAMA_WARM_ONLY=1 short-circuits main()). No second copy of the warm logic
# → no drift from warm_load_order().
WARM_SERVICE = "ollama-warm"
WARM_SCRIPT = "ollama-warm.py"


def render_warm_service(
    script_path: str, port: str, fast_model: str, timeout: int
) -> str:
    """Render the oneshot `.service` that re-warms after every ollama start (pure).

    `WantedBy=ollama.service` is the whole point: systemd pulls this in on every
    start of ollama.service, whatever caused it. It is a plain Wants with
    `After=` — never Requires/BindsTo/PartOf — so a warm that fails, hangs, or
    finds no model can never keep ollama from coming up."""
    return (
        "[Unit]\n"
        "Description=Ollama hot-path model warm-load (#1236)\n"
        "After=ollama.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Environment=OLLAMA_WARM_ONLY=1\n"
        f"Environment=OLLAMA_PORT={port}\n"
        f"Environment=OLLAMA_FAST_MODEL={fast_model}\n"
        f"Environment=OLLAMA_READINESS_TIMEOUT_SECONDS={timeout}\n"
        f"ExecStart=/usr/bin/env python3 {script_path}\n"
        "TimeoutStartSec=1800\n"
        "\n"
        "[Install]\n"
        "WantedBy=ollama.service\n"
    )


def install_warm_unit(data_dir: str, port: str, fast_model: str, timeout: int) -> bool:
    """Install the re-warm unit (#1236): copy THIS script to a durable data-dir
    path and write a `.service` wanted by `ollama.service`. Best-effort — a
    failure here never blocks the deploy. Returns True when the unit is enabled."""
    systemd_dir = os.path.expanduser("~/.config/systemd/user")
    script_dst = os.path.join(data_dir, "solarisbay", WARM_SCRIPT)
    try:
        with open(os.path.realpath(__file__), encoding="utf-8") as f:
            self_src = f.read()
    except OSError as e:
        jlog(
            "warn",
            "ollama:warm-unit",
            "could not read self for the warm unit",
            error=str(e),
        )
        return False
    try:
        os.makedirs(os.path.dirname(script_dst), exist_ok=True)
        with open(script_dst, "w", encoding="utf-8") as f:
            f.write(self_src)
        os.chmod(script_dst, 0o755)
        os.makedirs(systemd_dir, exist_ok=True)
        with open(
            os.path.join(systemd_dir, f"{WARM_SERVICE}.service"), "w", encoding="utf-8"
        ) as f:
            f.write(render_warm_service(script_dst, port, fast_model, timeout))
    except OSError as e:
        jlog("warn", "ollama:warm-unit", "could not write the warm unit", error=str(e))
        return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    enabled = subprocess.run(
        ["systemctl", "--user", "enable", f"{WARM_SERVICE}.service"],
        capture_output=True,
        text=True,
    )
    if enabled.returncode != 0:
        jlog(
            "warn",
            "ollama:warm-unit",
            "could not enable ollama-warm.service; only template deploys will warm",
            stderr=enabled.stderr[:400],
        )
        return False
    jlog(
        "info",
        "ollama:warm-unit",
        "ollama-warm.service enabled — every ollama start now re-warms the fast model",
        script=script_dst,
        fast_model=fast_model,
    )
    return True


def warm_only() -> int:
    """`OLLAMA_WARM_ONLY=1` entrypoint — what ollama-warm.service runs after
    every ollama start. Always exits 0: the warm is best-effort and a red unit
    would only add noise to a box whose ollama is otherwise healthy."""
    port = env("OLLAMA_PORT", "11434")
    fast_model = env("OLLAMA_FAST_MODEL", "gemma4:e4b")
    timeout = int(env("OLLAMA_READINESS_TIMEOUT_SECONDS", "600"))
    ollama_url = f"http://127.0.0.1:{port}"
    if not wait_for_ready(ollama_url, deadline_sec=min(timeout, 120)):
        jlog(
            "warn",
            "ollama:warm",
            "Ollama API not reachable; nothing warmed",
            url=ollama_url,
        )
        return 0
    warm_installed_models(ollama_url, fast_model, [])
    return 0


def register_http_check(sb_api: str, sb_token: str, ollama_url: str) -> None:
    """Best-effort: a non-200 here doesn't block the install."""
    headers = {}
    if sb_token:
        headers["X-SB-Internal-Token"] = sb_token
    status, body = http_request(
        f"{sb_api}/api/health/checks",
        payload={
            "id": "ollama-api",
            "name": "Ollama API",
            "type": "http",
            "target": f"{ollama_url}/api/tags",
            "interval": 60,
            "enabled": True,
            "httpConfig": {"expectedStatus": 200},
        },
        method="POST",
        timeout=10,
        extra_headers=headers,
    )
    if status == 200:
        jlog("info", "ollama:health", "registered http check ollama-api")
    else:
        jlog(
            "warn",
            "ollama:health",
            "could not register http check",
            status=status,
            body=body.decode("utf-8", errors="replace")[:200],
        )


def gpu_actually_engaged(ollama_url: str) -> bool:
    """Probe Ollama's /api/ps + the runtime config to decide whether the
    deployed unit actually got the GPU. `podman kube play` silently drops
    `resources.limits.nvidia.com/gpu` (#1026), so the .kube unit comes up
    on CPU even when OLLAMA_GPU_PASSTHROUGH=yes. The /api/version
    response doesn't expose VRAM, so we fall back to /api/show on the
    default model — when GPU is engaged, the runner-info has `runner: cuda`
    or similar in modern Ollama. If we can't determine, return False
    (caller assumes GPU isn't engaged and applies the Quadlet fixup)."""
    # Cheapest signal: /api/version returns 200 if the server is alive.
    # We trust /api/tags has already passed via wait_for_ready.
    # Most reliable: list loaded runners — when a model is loaded with
    # CUDA, /api/ps shows `processor: <gpu-id>`. With no model loaded,
    # there is no signal, so we don't gate on this; we rely on the
    # JSON-log inspection below.
    #
    # Fallback: read systemd journal output for the lib detection line.
    # The line we want is exactly:
    #   "inference compute" id=GPU-... library=CUDA ...
    # versus the CPU-only fallback:
    #   "inference compute" id=cpu library=cpu ...
    try:
        out = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                "ollama.service",
                "--since",
                "-2 min",
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "library=CUDA" in out.stdout or "library=ROCm" in out.stdout:
            return True
        if "library=cpu" in out.stdout:
            return False
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return False


def gpu_container_is_live_source() -> bool:
    """True iff the active `ollama.service` is generated from the GPU
    `.container` Quadlet (and not from the CPU `.kube`). Quadlet stamps the
    generated unit with `SourcePath=…/ollama.container` vs `…/ollama.kube`;
    we read it via `systemctl --user show -p SourcePath`. On a redeploy
    `podman kube play` re-creates `ollama.kube` and the active service flips
    back to the `.kube` source, so a byte-identical `.container` file on disk
    is NOT evidence the GPU unit is live — this is."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "SourcePath", "ollama.service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
    # `SourcePath=/home/core/.config/containers/systemd/ollama.container`
    return out.stdout.strip().endswith("ollama.container")


def render_gpu_container_unit(port: str, data_dir: str) -> str:
    """Render the `.container` Quadlet text for the GPU fixup. Mirrors the
    .yml's runtime contract (image, OLLAMA_HOST, hostNetwork, the volume
    mount) plus AddDevice + SecurityLabelDisable for CDI passthrough, and
    OLLAMA_CONTEXT_LENGTH + OLLAMA_FLASH_ATTENTION so the GPU path honors
    the same defaults as the .kube render path (#146 — the .kube env never
    reaches the GPU runtime, so anything required on the box has to be
    rendered here too). Kept pure so the needs-rewrite comparison and the
    write share one source of truth."""
    context_length = env("OLLAMA_CONTEXT_LENGTH", "32768")
    keep_alive = env("OLLAMA_KEEP_ALIVE", "15m")
    flash_attention = env("OLLAMA_FLASH_ATTENTION", "1")
    max_loaded_models = env("OLLAMA_MAX_LOADED_MODELS", "2")
    return (
        "[Unit]\n"
        "Description=Ollama (Local LLM Server, GPU passthrough #1026 fixup)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "Image=docker.io/ollama/ollama:latest\n"
        "ContainerName=ollama\n"
        "Network=host\n"
        f"Environment=OLLAMA_HOST=127.0.0.1:{port}\n"
        "# Force Ollama's DEFAULT load context. /v1/chat/completions ignores\n"
        "# per-request num_ctx, so only this env-set default lands — without\n"
        "# it the GPU Quadlet stays at 4096 and the engine loops at 1 token (#146).\n"
        f"Environment=OLLAMA_CONTEXT_LENGTH={context_length}\n"
        "# Fallback hold for consumers that send NO per-request keep_alive.\n"
        "# Short on purpose (#1264): this applies to every model anything on\n"
        "# this box loads, so as a blanket 24h one forgetful neighbour pinned\n"
        "# the GPU for a day. 15m is 3x Ollama's stock 5m — long enough that a\n"
        "# conversational pause doesn't reload mid-use — and equals the model\n"
        "# lease's 900s TTL, so a lease that is actually being used never\n"
        "# outlives its model's residency. Our own hot path doesn't rely on\n"
        "# this at all: the warm call pins the fast model explicitly (see\n"
        "# FAST_MODEL_KEEP_ALIVE).\n"
        f"Environment=OLLAMA_KEEP_ALIVE={keep_alive}\n"
        "# How many models stay resident (default 2): the household fast\n"
        "# chat and the embed model. The cap is GLOBAL — the embed model IS\n"
        "# counted (box-measured 2026-06-10). Only the fast model is\n"
        "# warm-loaded (#1258), so both slots belong to the voice hot path\n"
        "# and a big chat tag loaded on demand is what pays the reload.\n"
        f"Environment=OLLAMA_MAX_LOADED_MODELS={max_loaded_models}\n"
        "# Flash attention — negligible speed change here but harmless and\n"
        "# the prerequisite for optional KV-cache quant.\n"
        f"Environment=OLLAMA_FLASH_ATTENTION={flash_attention}\n"
        "# CDI device — verified working on rootless podman 5.8 + nvidia-ctk\n"
        "# 1.19. podman kube play silently drops this when expressed as\n"
        "# resources.limits.nvidia.com/gpu, which is why the .yml-based\n"
        "# deploy falls through to CPU. See #1026.\n"
        "AddDevice=nvidia.com/gpu=all\n"
        "# SELinux relaxation is required for NVML init on FCoS — without\n"
        "# it the container starts, sees the devices, but NVML returns\n"
        "# 'Insufficient Permissions' on every nvmlInit call.\n"
        "SecurityLabelDisable=true\n"
        f"Volume={data_dir}/ollama:/root/.ollama:Z\n"
        "AutoUpdate=registry\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_gpu_quadlet_fallback(port: str, data_dir: str) -> bool:
    """#1026 — Replace the just-deployed rootless `.kube` ollama unit
    with a `.container` Quadlet that uses `AddDevice=nvidia.com/gpu=all`
    + `SecurityLabelDisable=true`. That's the only combination on
    rootless podman 5.x that actually triggers CDI passthrough +
    SELinux relaxation for NVIDIA NVML init. Verified live on
    192.168.178.100 (RTX 2000 Ada): without this fixup ollama runs
    library=cpu with total_vram=0; with it, library=CUDA + 16 GiB
    VRAM and 78% GPU offload on gemma4:26b.

    Idempotent on both the file AND the active unit. The Quadlet is
    re-written only when its content drifts (#146: an install written
    before OLLAMA_CONTEXT_LENGTH support would otherwise skip forever).
    But a byte-identical `.container` file is NOT proof the GPU unit is
    live: a `podman kube play` redeploy re-creates `ollama.kube` and the
    active `ollama.service` flips back to the CPU `.kube` source (#322).
    So the content match guards only the redundant write — the stop →
    remove `ollama.kube` → daemon-reload → start activation still runs
    whenever the GPU `.container` isn't the live source or a `.kube`
    lingers. A genuinely-live GPU unit (matching file, `.container`
    source, no `.kube`) is the only no-op.

    Caveat: ServiceBay's discovery still tags `.container`-backed
    units as "unmanaged" (see agent.py — `is_managed` only when
    source_ext == .kube). The companion agent.py change in this PR
    widens that to .container so the dashboard reads correctly.
    """
    if not os.path.exists("/etc/cdi/nvidia.yaml"):
        jlog(
            "info",
            "ollama:gpu-fallback",
            "/etc/cdi/nvidia.yaml missing; CDI not registered on this host. Leaving CPU-only kube unit in place.",
        )
        return False

    systemd_dir = os.path.expanduser("~/.config/containers/systemd")
    kube_path = os.path.join(systemd_dir, "ollama.kube")
    container_path = os.path.join(systemd_dir, "ollama.container")

    container_unit = render_gpu_container_unit(port, data_dir)

    # Decide independently (a) whether the on-disk `.container` file already
    # matches what we'd render, and (b) whether the GPU `.container` is the
    # LIVE service source with no CPU `.kube` lingering. (a) gates only the
    # redundant file write; (b) is the authoritative activation check. A
    # redeploy re-creates `ollama.kube` and flips the active service back to
    # CPU even though the `.container` file is byte-identical (#322) — so a
    # content match alone must NOT short-circuit the stop→remove-kube→
    # daemon-reload→start activation below.
    content_matches = False
    if os.path.exists(container_path):
        try:
            with open(container_path) as f:
                existing = f.read()
        except OSError:
            existing = ""
        content_matches = existing == container_unit

    if (
        content_matches
        and gpu_container_is_live_source()
        and not os.path.exists(kube_path)
    ):
        jlog(
            "info",
            "ollama:gpu-fallback",
            "ollama.container already live (GPU source, no ollama.kube); no-op",
            path=container_path,
        )
        return True

    if content_matches:
        jlog(
            "info",
            "ollama:gpu-fallback",
            "ollama.container matches but GPU unit not live (ollama.kube re-created / service sourced from .kube #322); re-activating",
            path=container_path,
        )
    elif os.path.exists(container_path):
        jlog(
            "info",
            "ollama:gpu-fallback",
            "ollama.container present but stale (missing/old OLLAMA_CONTEXT_LENGTH #146); re-writing",
            path=container_path,
        )

    # 1. Stop the broken kube service (best-effort; it may already be down).
    subprocess.run(
        ["systemctl", "--user", "stop", "ollama.service"],
        check=False,
        capture_output=True,
    )

    # 2. Remove the .kube file so Quadlet doesn't generate a conflicting
    #    `ollama.service` from both sources at daemon-reload time.
    #    Keep ollama.yml around as documentation; nothing reads it once
    #    the .kube reference is gone.
    if os.path.exists(kube_path):
        try:
            os.unlink(kube_path)
        except OSError as e:
            jlog(
                "warn",
                "ollama:gpu-fallback",
                "could not remove ollama.kube — Quadlet may complain",
                path=kube_path,
                error=str(e),
            )

    # 3. Write the .container Quadlet (rendered above). Skip the write when
    #    the on-disk file already matches — we only reached here to re-activate
    #    (kube re-created), not because the content drifted (#322).
    if not content_matches:
        try:
            with open(container_path, "w") as f:
                f.write(container_unit)
            os.chmod(container_path, 0o644)
        except OSError as e:
            jlog(
                "error",
                "ollama:gpu-fallback",
                "could not write ollama.container",
                path=container_path,
                error=str(e),
            )
            return False

    # 4. Reload + start. Quadlet regenerates ollama.service from the new
    #    `.container` source on `daemon-reload`.
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    started = subprocess.run(
        ["systemctl", "--user", "start", "ollama.service"],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "error",
            "ollama:gpu-fallback",
            "systemctl start failed",
            stderr=started.stderr[:400],
        )
        return False

    jlog(
        "info",
        "ollama:gpu-fallback",
        "swapped rootless ollama.kube → ollama.container for CDI passthrough",
        path=container_path,
    )
    return True


def main() -> int:
    if env("OLLAMA_WARM_ONLY") == "1":
        return warm_only()

    port = env("OLLAMA_PORT", "11434")
    model = env("OLLAMA_DEFAULT_MODEL", "gemma4:12b")
    extra_models_raw = env("OLLAMA_EXTRA_MODELS", "")
    extra_models = [m.strip() for m in extra_models_raw.split(",") if m.strip()]
    vision_model = env("OLLAMA_VISION_MODEL", "")
    embed_model = env("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    fast_model = env("OLLAMA_FAST_MODEL", "gemma4:e4b")
    timeout = int(env("OLLAMA_READINESS_TIMEOUT_SECONDS", "600"))
    sb_api = env("SB_API_URL", "http://localhost:3000")
    sb_token = env("SB_API_TOKEN", "")
    # Blank/unset => auto-detect: engage the GPU when the host has a
    # CDI-registered NVIDIA device, the same file install_gpu_quadlet_fallback
    # gates on. Explicit yes/no overrides the probe either way.
    _gpu = env("OLLAMA_GPU_PASSTHROUGH", "").strip().lower()
    if _gpu in ("yes", "true", "1"):
        gpu_requested = True
    elif _gpu in ("no", "false", "0", "off"):
        gpu_requested = False
    else:
        gpu_requested = os.path.exists("/etc/cdi/nvidia.yaml")
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    ollama_url = f"http://127.0.0.1:{port}"

    # #1026 — GPU fixup runs BEFORE wait_for_ready so any model pull
    # below loads onto the GPU-backed runtime, not the broken CPU one.
    if gpu_requested:
        if not gpu_actually_engaged(ollama_url):
            jlog(
                "info",
                "ollama:bootstrap",
                "GPU passthrough requested but the .kube unit fell through to CPU — applying #1026 Quadlet fixup",
            )
            install_gpu_quadlet_fallback(port, data_dir)
        else:
            jlog(
                "info", "ollama:bootstrap", "GPU already engaged; no #1026 fixup needed"
            )

    jlog(
        "info",
        "ollama:bootstrap",
        "waiting for Ollama API",
        url=ollama_url,
        deadline_sec=timeout,
    )
    if not wait_for_ready(ollama_url, deadline_sec=min(timeout, 120)):
        jlog(
            "warn",
            "ollama:bootstrap",
            "Ollama API not reachable yet; skipping model pull. The service may still come up — check the install log and re-run from the wizard if needed.",
            url=ollama_url,
        )
        return 0

    if model:
        jlog("info", "ollama:pull", "starting model pull", model=model)
        ok = pull_model(ollama_url, model, stall_sec=timeout)
        if not ok:
            jlog(
                "warn",
                "ollama:pull",
                'model pull did not complete; the pod is up but the default model is missing. Pull manually with `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'`.'
                % (port, model),
                model=model,
            )

    # Extras (#1046): one-click-switchable alternatives the operator can
    # pick from the Solaris Engine's model settings without a fresh download. Failures are
    # warn-not-fatal — the default model is the only one the install
    # depends on; extras enrich the choice set.
    for extra in extra_models:
        if extra == model:
            continue  # already covered above
        jlog("info", "ollama:pull", "starting extra-model pull", model=extra)
        if not pull_model(ollama_url, extra, stall_sec=timeout):
            jlog(
                "warn",
                "ollama:pull",
                'extra-model pull did not complete; it will not be selectable from the Solaris Engine\'s model settings until pulled manually. Run `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'`.'
                % (port, extra),
                model=extra,
            )

    if vision_model:
        jlog("info", "ollama:pull", "starting vision-model pull", model=vision_model)
        ok = pull_model(ollama_url, vision_model, stall_sec=timeout)
        if not ok:
            jlog(
                "warn",
                "ollama:pull",
                'vision-model pull did not complete; Solaris\'s media-ingestion-multimodal skill will fall back to text-only. Pull manually with `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'` or bump OLLAMA_READINESS_TIMEOUT_SECONDS.'
                % (port, vision_model),
                model=vision_model,
            )

    # Dedicated embedding model (#214): a distinct tag gets its own
    # llama-server runner, so embed/RAG requests run in parallel with a
    # chat generation instead of serializing behind it. Embeddings must
    # target this tag, never the chat model.
    if embed_model and embed_model not in (model, *extra_models):
        jlog("info", "ollama:pull", "starting embed-model pull", model=embed_model)
        if not pull_model(ollama_url, embed_model, stall_sec=timeout):
            jlog(
                "warn",
                "ollama:pull",
                'embed-model pull did not complete; embeddings/RAG will have no resident embed model. Pull manually with `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'`.'
                % (port, embed_model),
                model=embed_model,
            )

    # Bind the warm to the ollama unit's own start (#1236) so an auto-update,
    # a reboot or a GPU-Quadlet force-recreate re-warms too, then warm now for
    # this deploy.
    install_warm_unit(data_dir, port, fast_model, timeout)
    warm_installed_models(ollama_url, fast_model, [*extra_models, model])

    register_http_check(sb_api, sb_token, ollama_url)

    print(f"✅ Ollama is running on 127.0.0.1:{port}. Default model: {model}.")
    if extra_models:
        print(f"   Extra models pulled: {', '.join(extra_models)}.")
    if vision_model:
        print(f"   Vision model: {vision_model} (multimodal-capable).")
    if embed_model:
        print(
            f"   Embedding model: {embed_model} (target this for RAG, not the chat model)."
        )
    print(
        f"   Other ServiceBay templates (solaris, solarisbay) can reach it at http://127.0.0.1:{port}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
