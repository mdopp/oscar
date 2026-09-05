#!/usr/bin/env python3
"""
post-deploy hook for the `llama` template.

Four responsibilities:

  1. **Download the GGUFs.** llama-server serves a file, not a registry —
     nothing pulls on first start. The weights, Google's MTP drafter and the
     multimodal projector are fetched from Hugging Face into
     ${DATA_DIR}/llama/models before the server is expected to come up.

  2. **Get the container onto the GPU.** `podman kube play` drops
     `resources.limits.nvidia.com/gpu`, and on rootless FCoS the CDI device
     alone is not enough — without `SecurityLabelDisable=true` llama-server
     logs one passing "no usable GPU found" warning and answers from the CPU.
     Same fixup the ollama template carries (#1026), same two lines.

  3. **Register an HTTP health check** against `/health`, which returns 200
     only once both the model and the drafter are loaded.

  4. **Install the GPU lease** (#1320, #1319, #1325). A copy of this script
     lands at `${DATA_DIR}/solarisbay/gpu-lease.py`; run with `acquire
     <holder>` it hands the whole card to another job, with `release` it gives
     it back. Self-copy, like ollama-warm (#1236), so the unit list cannot
     drift from a second copy of itself. `--model coding` and `--model
     foundry` take the softer path: llama-server is reloaded with that
     profile's model instead of stopped, and Solaris answers the household
     from it.

Idempotent: a second run finds the files on disk and skips the download; the
Quadlet is re-activated only when it isn't the live unit source; the
health-check API does upsert-by-id; the lease script is rewritten in place.

See lib/registry.ts:getTemplatePostDeployScript for the script protocol and
docs/TEMPLATE_AUTHORING.md § Health checks for the check-registration
contract.
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

PROGRESS_LOG_INTERVAL_SEC = 15
DOWNLOAD_CHUNK = 1024 * 1024


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


def model_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}?download=true"


def download_model(repo: str, filename: str, models_dir: str, stall_sec: int) -> bool:
    """Fetch one GGUF into `models_dir`, unless it is already there.

    Writes to `<name>.part` and renames on completion, so an interrupted
    download can never be mistaken for a usable model file — llama-server
    would otherwise start against a truncated GGUF and crash-loop with a
    parse error that says nothing about the real cause.
    """
    dest = os.path.join(models_dir, filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        jlog(
            "info",
            "llama:models",
            "model file already present",
            file=filename,
            size_mb=os.path.getsize(dest) // (1024 * 1024),
        )
        return True
    part = f"{dest}.part"
    url = model_url(repo, filename)
    jlog("info", "llama:models", "downloading model file", file=filename, url=url)
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "solarisbay"})
        with (
            urllib.request.urlopen(req, timeout=stall_sec) as resp,
            open(part, "wb") as out,
        ):
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_log = 0.0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_log >= PROGRESS_LOG_INTERVAL_SEC:
                    pct = int(done * 100 / total) if total else 0
                    filled = pct * 20 // 100
                    bar = "#" * filled + "-" * (20 - filled)
                    jlog(
                        "info",
                        "llama:models",
                        f"{filename} [{bar}] {pct}% "
                        f"({done // (1024 * 1024)}/{total // (1024 * 1024)} MB)",
                        file=filename,
                        percent=pct,
                        completed_mb=done // (1024 * 1024),
                        total_mb=total // (1024 * 1024),
                    )
                    last_log = now
        if total and done < total:
            raise OSError(f"short read: {done} of {total} bytes")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        jlog(
            "error",
            "llama:models",
            "download failed",
            file=filename,
            url=url,
            error=str(e),
        )
        try:
            os.unlink(part)
        except OSError:
            pass
        return False
    os.replace(part, dest)
    jlog(
        "info",
        "llama:models",
        "model file ready",
        file=filename,
        size_mb=os.path.getsize(dest) // (1024 * 1024),
        elapsed_sec=int(time.time() - started),
    )
    return True


def wait_for_ready(llama_url: str, deadline_sec: int) -> bool:
    """Poll /health until llama-server answers 200 (model + drafter loaded)."""
    started = time.time()
    last_beat = 0.0
    while time.time() - started < deadline_sec:
        status, _ = http_request(f"{llama_url}/health", timeout=5)
        if status == 200:
            return True
        elapsed = time.time() - started
        if elapsed - last_beat >= 10:
            jlog(
                "info",
                "llama:wait",
                "still waiting for llama-server",
                elapsed_sec=int(elapsed),
            )
            last_beat = elapsed
        time.sleep(3)
    return False


def speculative_active(llama_url: str) -> bool:
    """True when /slots reports the drafter is actually in play.

    The server starts happily without speculative decoding when the draft
    model is missing or the flags are wrong, and then just runs at half
    speed — a silent regression with no error anywhere (#1317/#1318).
    """
    status, body = http_request(f"{llama_url}/slots", timeout=5)
    if status != 200:
        return False
    try:
        slots = json.loads(body.decode("utf-8") or "[]")
    except json.JSONDecodeError:
        return False
    return any(bool(s.get("speculative")) for s in slots if isinstance(s, dict))


def gpu_container_is_live_source() -> bool:
    """True iff the active `llama.service` is generated from the GPU
    `.container` Quadlet and not from the CPU `.kube`. A redeploy re-creates
    `llama.kube` and flips the active service back, so a byte-identical
    `.container` file on disk is not evidence the GPU unit is live — this is."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "SourcePath", "llama.service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
    return out.stdout.strip().endswith("llama.container")


def env_profile() -> dict[str, str]:
    """The household server profile, as the template variables describe it."""
    return {
        "model_repo": env("LLAMA_MODEL_REPO", "ggml-org/gemma-4-E4B-it-GGUF"),
        "model_file": env("LLAMA_MODEL_FILE", "gemma-4-E4B-it-Q4_0.gguf"),
        "draft_repo": "",
        "draft_file": env("LLAMA_DRAFT_FILE", "mtp-gemma-4-E4B-it-Q8_0.gguf"),
        "mmproj_file": env("LLAMA_MMPROJ_FILE", ""),
        "context_length": env("LLAMA_CONTEXT_LENGTH", "32768"),
        "draft_n_max": env("LLAMA_DRAFT_N_MAX", "4"),
        "cache_type": "",
        "parallel": "",
        "label": "Gemma 4 E4B",
    }


def server_args(
    port: str, models_dir_in_container: str, profile: dict[str, str] | None = None
) -> list[str]:
    """The llama-server argv, shared by the Quadlet render and the log line.

    Mirrors template.yml's `args`. `--spec-type draft-mtp` is mandatory for
    the MTP drafter and `--draft-max` no longer exists — the current image
    refuses to start on it ("the argument has been removed").
    """
    profile = profile or env_profile()
    model_file = profile["model_file"]
    draft_file = profile["draft_file"]
    mmproj_file = profile["mmproj_file"]
    context_length = profile["context_length"]
    draft_n_max = profile["draft_n_max"]
    args = [
        "--host",
        "127.0.0.1",
        "--port",
        port,
        "-m",
        f"{models_dir_in_container}/{model_file}",
        "-ngl",
        "99",
        "-c",
        context_length,
        "--jinja",
    ]
    # Both only appear for a profile that measured as needing them: the coding
    # model's 64 recurrent layers cost 748 MiB of state PER SEQUENCE, so with
    # llama-server's stock 4 slots the drafter OOMs before it loads, and f16 KV
    # at 65k costs the 910 MiB the drafter needs (#1318, cell H1).
    if profile["cache_type"]:
        args += ["-ctk", profile["cache_type"], "-ctv", profile["cache_type"]]
    if profile["parallel"]:
        args += ["--parallel", profile["parallel"]]
    if draft_file:
        args += [
            "--spec-type",
            "draft-mtp",
            "--spec-draft-model",
            f"{models_dir_in_container}/{draft_file}",
            "--spec-draft-ngl",
            "99",
            "--spec-draft-n-max",
            draft_n_max,
        ]
    if mmproj_file:
        args += ["--mmproj", f"{models_dir_in_container}/{mmproj_file}"]
    return args


def render_gpu_container_unit(
    port: str, data_dir: str, profile: dict[str, str] | None = None
) -> str:
    """Render the `.container` Quadlet text for the GPU fixup. Pure, so the
    needs-rewrite comparison and the write share one source of truth."""
    exec_args = " ".join(server_args(port, "/models", profile))
    return (
        "[Unit]\n"
        "Description=llama.cpp llama-server (household model, GPU passthrough)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "Image=ghcr.io/ggml-org/llama.cpp:server-cuda\n"
        "ContainerName=llama\n"
        "Network=host\n"
        f"Exec={exec_args}\n"
        "# CDI device — podman kube play silently drops this when it is\n"
        "# expressed as resources.limits.nvidia.com/gpu, which is why the\n"
        "# .yml-based deploy falls through to CPU. See #1026.\n"
        "AddDevice=nvidia.com/gpu=all\n"
        "# Without the SELinux relaxation the container sees the device but\n"
        "# NVML cannot init: llama-server logs one passing 'no usable GPU\n"
        "# found', loads the model into RAM and answers from the CPU at a\n"
        "# fraction of the speed, with nothing in any log that reads as an\n"
        "# error. Box-measured 2026-09-04 (#1318).\n"
        "SecurityLabelDisable=true\n"
        f"Volume={data_dir}/llama/models:/models:Z\n"
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
    """Replace the deployed rootless `.kube` llama unit with a `.container`
    Quadlet carrying `AddDevice=nvidia.com/gpu=all` + `SecurityLabelDisable=
    true` — the only combination on rootless podman 5.x that actually gets
    CDI passthrough plus the SELinux relaxation NVML init needs.

    Idempotent on both the file and the active unit: a matching file whose
    `.container` is the live source with no `.kube` lingering is the only
    no-op."""
    if not os.path.exists("/etc/cdi/nvidia.yaml"):
        jlog(
            "info",
            "llama:gpu-fallback",
            "/etc/cdi/nvidia.yaml missing; CDI not registered on this host. Leaving the CPU-only kube unit in place.",
        )
        return False

    systemd_dir = os.path.expanduser("~/.config/containers/systemd")
    kube_path = os.path.join(systemd_dir, "llama.kube")
    container_path = os.path.join(systemd_dir, "llama.container")
    container_unit = render_gpu_container_unit(port, data_dir)

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
            "llama:gpu-fallback",
            "llama.container already live (GPU source, no llama.kube); no-op",
            path=container_path,
        )
        return True

    subprocess.run(
        ["systemctl", "--user", "stop", "llama.service"],
        check=False,
        capture_output=True,
    )
    if os.path.exists(kube_path):
        try:
            os.unlink(kube_path)
        except OSError as e:
            jlog(
                "warn",
                "llama:gpu-fallback",
                "could not remove llama.kube — Quadlet may complain",
                path=kube_path,
                error=str(e),
            )
    if not content_matches:
        try:
            with open(container_path, "w") as f:
                f.write(container_unit)
            os.chmod(container_path, 0o644)
        except OSError as e:
            jlog(
                "error",
                "llama:gpu-fallback",
                "could not write llama.container",
                path=container_path,
                error=str(e),
            )
            return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    started = subprocess.run(
        ["systemctl", "--user", "start", "llama.service"],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "error",
            "llama:gpu-fallback",
            "systemctl start failed",
            stderr=started.stderr[:400],
        )
        return False
    jlog(
        "info",
        "llama:gpu-fallback",
        "swapped rootless llama.kube -> llama.container for CDI passthrough",
        path=container_path,
    )
    return True


# --- The whole-card GPU lease (#1320) -------------------------------------
#
# Box-measured over the night of 04./05.09. (#1318): the coding run's Qwen 27B
# peaks at 15 004 MiB of 16 380 — it does not fit beside Solaris' own e4b
# server (3 866 MiB), let alone the voice stack. The operator's decision is
# that such a job takes the card on request, with no time window and no
# presence check, and that Solaris answers honestly meanwhile.
#
# So a lease is: write the file, stop everything that holds VRAM. And a
# release is the same in reverse, with the file removed last — while it is
# there `solaris_chat.gpu_lease` makes the Engine say it is busy instead of
# talking into a dead socket.
LEASE_SCRIPT = "gpu-lease.py"
LEASE_FILE = "gpu_lease.json"
PROFILE_FILE = "llama-profile.json"

# `ollama.service` also pulls `ollama-warm.service` back in on start (#1236),
# which is what re-warms Ollama's own fast model; llama-server warms by
# loading at startup, which is what `release` waits for.
#
# The two voice units are listed apart because the coding lease (#1319) keeps
# them RUNNING, on the CPU: the operator ruled on 2026-09-05 that the house can
# still be spoken to during a coding window, slower rather than not at all. A
# foundry lease (#1325) stops none of the five and leaves them all on the GPU.
LEASE_GPU_UNITS = (
    "ollama.service",
    "solaris-whisper-batch.service",
    "solaris-wakeword-trainer.service",
)
LEASE_VOICE_UNITS = (
    "solaris-whisper.service",
    "solaris-tts.service",
)
LEASED_UNITS = LEASE_GPU_UNITS + LEASE_VOICE_UNITS + ("llama.service",)

# Which execution provider the two voice units use, read from this file by
# their Quadlets (`EnvironmentFile=`). The other half of this contract is
# `templates/solaris/post-deploy.py`'s VOICE_DEVICE_* — the file is written
# there at install and flipped here for the duration of a coding lease;
# templates/tests/test_gpu_lease.py pins the two halves together.
VOICE_DEVICE_FILE = "voice-device.env"
VOICE_DEVICE_ENV = {
    "gpu": "WHISPER_DEVICE=cuda\nKOKORO_ONNX_PROVIDER=cuda\n",
    "cpu": "WHISPER_DEVICE=cpu\nKOKORO_ONNX_PROVIDER=cpu\n",
}

# The coding-lease server profile (#1319). Box-measured 2026-09-04 (#1318,
# cell H1): 15 004 of 16 380 MiB, 32.6 tok/s, tool calls 12/12, no thinking
# leak, drafter acceptance 71.4%. `--parallel 1` and q8 KV are not tuning —
# with llama-server's stock 4 slots or f16 KV the drafter never loads at all.
CODING_PROFILE = {
    "model_repo": "unsloth/Qwen3.8-27B-GGUF",
    "model_file": "Qwen3.8-27B-UD-IQ3_XXS.gguf",
    "draft_repo": "ggml-org/Qwen3.8-27B-GGUF",
    "draft_file": "mtp-Qwen3.8-27B-Q4_0.gguf",
    "mmproj_file": "",
    "context_length": "65536",
    "draft_n_max": "4",
    "cache_type": "q8_0",
    "parallel": "1",
    "label": "Qwen 3.8 27B",
}

# The foundry-lease server profile (#1325). Box-measured 2026-09-04 (#1318,
# cell K2): 9 626 MiB steady / 9 636 peak with four slots and f16 KV at 32k,
# 36.6 tok/s, 1.53 s per finished answer, tool calls 6/6, no thinking leak.
# Beside the voice stack under load (4 508 MiB, #1260) that is 14 144 of
# 16 380 — which only holds because llama-server runs the 12B *instead of* the
# household e4b (3 872 MiB): all three together are 18 016 and do not fit.
# No mmproj: the 12B repo's vision projector has never been fetched or
# measured on this box, and a file that turns out not to exist would refuse
# the lease outright. A photo reaches the 12B as text for the window.
FOUNDRY_PROFILE = {
    "model_repo": "ggml-org/gemma-4-12B-it-GGUF",
    "model_file": "gemma-4-12B-it-Q4_0.gguf",
    "draft_repo": "ggml-org/gemma-4-12B-it-GGUF",
    "draft_file": "mtp-gemma-4-12B-it-Q8_0.gguf",
    "mmproj_file": "",
    "context_length": "32768",
    "draft_n_max": "4",
    "cache_type": "",
    "parallel": "",
    "label": "Gemma 4 12B",
}

# The profiles that swap llama-server instead of emptying the card. Without
# `--model` the lease is exclusive: everything stops and nothing answers.
LEASE_PROFILES = {"coding": CODING_PROFILE, "foundry": FOUNDRY_PROFILE}

# Ollama stays up through a foundry lease — the household still needs its
# embeddings and the vision ingest — but it must not go on holding a chat
# model: its warm e4b costs 4 798 MiB (box idle 6 532 minus the voice stack's
# 1 734), which is more than the 12B has to spare. `ollama-warm.service`
# (#1236) puts it back at release.
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_WARM_UNIT = "ollama-warm.service"

# How long `release` waits for the household model to answer /health again.
# Cold e4b was ~38 s in the night measurements; this is the give-up point,
# after which the lease file is dropped anyway rather than muting Solaris.
LEASE_WARM_DEADLINE_SEC = 300

# Every lease carries a deadline (#1319, precedent #1260): an end signal alone
# is not enough, because a coding run that dies without releasing would leave
# the household on the coding model — or, in exclusive mode, mute — until
# someone notices. A transient systemd timer runs `release` at the deadline.
LEASE_DEFAULT_DURATION_SEC = 4 * 3600
LEASE_EXPIRY_UNIT = "solaris-gpu-lease-expiry"


def lease_file(data_dir: str) -> str:
    """The lease file, on the volume the chat pod mounts at /var/lib/solaris."""
    return os.path.join(data_dir, "solarisbay", LEASE_FILE)


def profile_file(data_dir: str) -> str:
    return os.path.join(data_dir, "solarisbay", PROFILE_FILE)


def voice_device_file(data_dir: str) -> str:
    return os.path.join(data_dir, "solarisbay", VOICE_DEVICE_FILE)


def save_household_profile(data_dir: str) -> None:
    """Record the installed household profile, so a `release` restores what the
    operator actually deployed instead of this script's own defaults."""
    path = profile_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(env_profile(), f)
    except OSError as e:
        jlog(
            "warn",
            "llama:lease",
            "could not record the household profile",
            path=path,
            error=str(e),
        )


def household_profile(data_dir: str) -> dict[str, str]:
    """The profile `release` reloads: the recorded one, else this script's
    defaults (which is what a box installed before #1319 has)."""
    profile = env_profile()
    try:
        with open(profile_file(data_dir), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return profile
    if isinstance(saved, dict):
        profile.update({k: str(v) for k, v in saved.items() if k in profile})
    return profile


def parse_duration(text: str) -> int:
    """`4h` / `90m` / `3600` → seconds. 0 for anything unreadable."""
    text = text.strip().lower()
    factor = 1
    if text.endswith("h"):
        factor, text = 3600, text[:-1]
    elif text.endswith("m"):
        factor, text = 60, text[:-1]
    elif text.endswith("s"):
        text = text[:-1]
    try:
        seconds = int(float(text) * factor)
    except ValueError:
        return 0
    return seconds if seconds > 0 else 0


def read_lease(data_dir: str) -> dict[str, object]:
    try:
        with open(lease_file(data_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def systemctl(verb: str, units: tuple[str, ...]) -> bool:
    out = subprocess.run(
        ["systemctl", "--user", verb, *units],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        jlog(
            "warn",
            "llama:lease",
            f"systemctl {verb} reported a failure",
            units=list(units),
            stderr=out.stderr[:400],
        )
    return out.returncode == 0


def write_lease(data_dir: str, record: dict[str, object]) -> bool:
    path = lease_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error", "llama:lease", "could not write the lease", path=path, error=str(e)
        )
        return False
    return True


def set_voice_device(data_dir: str, device: str) -> None:
    """Put the two voice units on `cuda` or `cpu` and restart them.

    An env file rather than a second pair of units: the household's whisper
    model, prompt, health probe and Wyoming ports are one definition either way,
    and only the execution provider moves (#1319)."""
    path = voice_device_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(VOICE_DEVICE_ENV[device])
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:lease",
            "could not switch the voice units; leaving them as they are",
            path=path,
            device=device,
            error=str(e),
        )
        return
    systemctl("restart", LEASE_VOICE_UNITS)
    jlog(
        "info",
        "llama:lease",
        f"voice stack switched to {device}",
        units=list(LEASE_VOICE_UNITS),
    )


def unload_ollama_models() -> None:
    """Ask Ollama to drop every model it holds resident, keeping the service.

    `keep_alive: 0` on a bare generate call is Ollama's own unload path, so the
    runner exits and gives the VRAM back without the unit going down with it.
    """
    status, body = http_request(f"{OLLAMA_URL}/api/ps", timeout=10)
    if status != 200:
        jlog("warn", "llama:lease", "could not ask Ollama what it holds", status=status)
        return
    try:
        loaded = json.loads(body.decode("utf-8") or "{}").get("models") or []
    except ValueError:
        return
    for entry in loaded:
        name = (
            entry.get("model") or entry.get("name") if isinstance(entry, dict) else ""
        )
        if not name:
            continue
        http_request(
            f"{OLLAMA_URL}/api/generate",
            payload={"model": name, "keep_alive": 0},
            method="POST",
            timeout=60,
        )
        jlog("info", "llama:lease", "asked Ollama to unload a model", model=name)


def rewarm_ollama() -> None:
    systemctl("restart", (OLLAMA_WARM_UNIT,))


def apply_llama_profile(port: str, data_dir: str, profile: dict[str, str]) -> None:
    """Reload llama-server on `profile` — rewrite its Quadlet and restart it."""
    container_path = os.path.expanduser("~/.config/containers/systemd/llama.container")
    try:
        with open(container_path, "w", encoding="utf-8") as f:
            f.write(render_gpu_container_unit(port, data_dir, profile))
        os.chmod(container_path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:lease",
            "could not rewrite llama.container",
            path=container_path,
            error=str(e),
        )
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    systemctl("restart", ("llama.service",))
    jlog(
        "info",
        "llama:lease",
        "llama-server reloading",
        model=profile["label"],
        model_file=profile["model_file"],
    )


def schedule_expiry(data_dir: str, port: str, seconds: int) -> None:
    """Arm the transient timer that releases the card at the deadline."""
    cancel_expiry()
    out = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={LEASE_EXPIRY_UNIT}",
            f"--on-active={seconds}",
            "--description=Solaris GPU lease expiry (#1319)",
            f"--setenv=DATA_DIR={data_dir}",
            f"--setenv=LLAMA_PORT={port}",
            sys.executable,
            os.path.realpath(__file__),
            "release",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        jlog(
            "error",
            "llama:lease",
            "could not arm the expiry timer — release the card by hand when the job is done",
            stderr=out.stderr[:400],
        )
        return
    jlog("info", "llama:lease", "expiry armed", seconds=seconds)


def cancel_expiry() -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", f"{LEASE_EXPIRY_UNIT}.timer"],
        check=False,
        capture_output=True,
    )


def lease_acquire(
    data_dir: str,
    holder: str,
    port: str = "11435",
    model: str = "",
    duration_sec: int = LEASE_DEFAULT_DURATION_SEC,
) -> int:
    """Hand the card to `holder`: claim, then stop.

    `model=coding` (#1319) and `model=foundry` (#1325) are the softer variants:
    llama-server is reloaded on that profile's model instead of stopped and
    Solaris answers the household from it for the window. A coding lease also
    stops Ollama, the batch transcriber and the trainer and moves the voice
    stack to the CPU; a foundry lease leaves all five units alone on the GPU,
    because foundry transcribes through `solaris-whisper-batch` while it runs.
    Without `--model` the card is emptied outright.
    """
    current = read_lease(data_dir)
    if current and current.get("holder") != holder:
        jlog(
            "error",
            "llama:lease",
            "the card is already leased; release it first",
            holder=current.get("holder"),
            requested_by=holder,
        )
        return 1
    if model and model not in LEASE_PROFILES:
        jlog(
            "error",
            "llama:lease",
            "unknown --model; known: " + ", ".join(sorted(LEASE_PROFILES)),
            model=model,
        )
        return 2
    profile = LEASE_PROFILES.get(model)
    if profile:
        if not gpu_container_is_live_source():
            # The swap rewrites llama.container. If llama.service is still the
            # deployed .kube unit, that file is inert and the restart would
            # quietly bring the household model back up instead.
            jlog(
                "error",
                "llama:lease",
                f"llama.service is not the GPU .container unit, so the {model} profile cannot be swapped in; nothing was stopped",
            )
            return 1
        # Before anything stops: 12.6 GB over a household line is not something
        # to do with the house muted, and a second acquire finds the files.
        models_dir = os.path.join(data_dir, "llama", "models")
        stall_sec = int(env("LLAMA_DOWNLOAD_STALL_SECONDS", "600"))
        for repo_key, file_key in (
            ("model_repo", "model_file"),
            ("draft_repo", "draft_file"),
        ):
            if not download_model(
                profile[repo_key], profile[file_key], models_dir, stall_sec
            ):
                jlog(
                    "error",
                    "llama:lease",
                    f"the {model} weights are not on the box; nothing was stopped",
                    file=profile[file_key],
                )
                return 1
    now = time.time()
    if not write_lease(
        data_dir,
        {
            "holder": holder,
            "since": now,
            "until": now + duration_sec,
            "mode": model or "exclusive",
            "model": profile["label"] if profile else "",
            # Flipped once the leased model answers /health. Until then the
            # card is in the swap and the Engine still says it is busy.
            "ready": False,
        },
    ):
        return 1
    # Claim before stopping: in the gap Solaris already says it is busy. The
    # other order leaves a window where the server is gone and nothing knows.
    schedule_expiry(data_dir, port, duration_sec)
    if not profile:
        systemctl("stop", LEASED_UNITS)
        jlog(
            "info",
            "llama:lease",
            "GPU leased — voice stack, Ollama and llama-server stopped",
            holder=holder,
            units=list(LEASED_UNITS),
            until_sec=int(now + duration_sec),
        )
        return 0
    if model == "coding":
        systemctl("stop", LEASE_GPU_UNITS)
        set_voice_device(data_dir, "cpu")
    else:
        unload_ollama_models()
    apply_llama_profile(port, data_dir, profile)
    llama_url = f"http://127.0.0.1:{port}"
    if not wait_for_ready(llama_url, deadline_sec=LEASE_WARM_DEADLINE_SEC):
        jlog(
            "error",
            "llama:lease",
            "the leased model did not answer /health; releasing the card again",
            model=profile["label"],
            url=llama_url,
        )
        lease_release(data_dir, port)
        return 1
    if not speculative_active(llama_url):
        jlog(
            "warn",
            "llama:lease",
            "the leased model is up but /slots reports no speculative decoding — check the drafter; answers will be about a third slower",
        )
    current = read_lease(data_dir)
    current["ready"] = True
    write_lease(data_dir, current)
    jlog(
        "info",
        "llama:lease",
        f"GPU leased for {model} — Solaris keeps answering, from the leased model",
        holder=holder,
        model=profile["label"],
        voice="cpu" if model == "coding" else "gpu",
        until_sec=int(now + duration_sec),
    )
    return 0


def lease_release(data_dir: str, port: str) -> int:
    """Give the card back: start everything, wait for the household model, drop
    the lease last so nobody is told "ready" while e4b is still loading."""
    mode = read_lease(data_dir).get("mode")
    cancel_expiry()
    if mode == "coding":
        systemctl("start", LEASE_GPU_UNITS)
        set_voice_device(data_dir, "gpu")
        apply_llama_profile(port, data_dir, household_profile(data_dir))
    elif mode == "foundry":
        apply_llama_profile(port, data_dir, household_profile(data_dir))
        rewarm_ollama()
    else:
        systemctl("start", LEASED_UNITS)
    llama_url = f"http://127.0.0.1:{port}"
    warm = wait_for_ready(llama_url, deadline_sec=LEASE_WARM_DEADLINE_SEC)
    try:
        os.unlink(lease_file(data_dir))
    except OSError:
        pass
    if not warm:
        jlog(
            "warn",
            "llama:lease",
            "units restarted but llama-server did not answer /health; the lease is cleared anyway so Solaris stops saying it is busy. Check `journalctl --user -u llama.service`.",
            url=llama_url,
        )
        return 1
    if not speculative_active(llama_url):
        jlog(
            "warn",
            "llama:lease",
            "household model is back but /slots reports no speculative decoding — answers will take about twice as long",
        )
    jlog("info", "llama:lease", "GPU released — household model warm again")
    return 0


def lease_cli(argv: list[str]) -> int:
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    port = env("LLAMA_PORT", "11435")
    if argv[0] != "acquire":
        return lease_release(data_dir, port)
    holder, model, duration = "", "", LEASE_DEFAULT_DURATION_SEC
    rest = argv[1:]
    while rest:
        token = rest.pop(0)
        if token in ("--model", "--duration"):
            value = rest.pop(0) if rest else ""
            if token == "--model":
                model = value.strip()
            else:
                duration = parse_duration(value)
        elif not holder:
            holder = token.strip()
    if not holder or not duration:
        jlog(
            "error",
            "llama:lease",
            "usage: gpu-lease.py acquire <holder> [--model coding|foundry] [--duration 4h]",
        )
        return 2
    return lease_acquire(data_dir, holder, port, model, duration)


def install_lease_script(data_dir: str) -> str:
    """Copy this script to a durable path so foundry and the coding run can
    call it. Same self-copy as ollama-warm (#1236): one source of truth for
    the unit list, and no second file to fall out of step with it."""
    dst = os.path.join(data_dir, "solarisbay", LEASE_SCRIPT)
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
            "llama:lease",
            "could not install the gpu-lease script",
            path=dst,
            error=str(e),
        )
        return ""
    jlog("info", "llama:lease", "gpu-lease installed", path=dst)
    return dst


def register_http_check(sb_api: str, sb_token: str, llama_url: str) -> None:
    """Best-effort: a non-200 here doesn't block the install."""
    headers = {}
    if sb_token:
        headers["X-SB-Internal-Token"] = sb_token
    status, body = http_request(
        f"{sb_api}/api/health/checks",
        payload={
            "id": "llama-api",
            "name": "llama.cpp API",
            "type": "http",
            "target": f"{llama_url}/health",
            "interval": 60,
            "enabled": True,
            "httpConfig": {"expectedStatus": 200},
        },
        method="POST",
        timeout=10,
        extra_headers=headers,
    )
    if status == 200:
        jlog("info", "llama:health", "registered http check llama-api")
    else:
        jlog(
            "warn",
            "llama:health",
            "could not register http check",
            status=status,
            body=body.decode("utf-8", errors="replace")[:200],
        )


def main() -> int:
    # The lease entrypoint (#1320). Gated on the exact verb rather than on
    # "any argv", so a future ServiceBay that passes the script an argument
    # still installs instead of trying to move the GPU.
    if len(sys.argv) > 1 and sys.argv[1] in ("acquire", "release"):
        return lease_cli(sys.argv[1:])

    port = env("LLAMA_PORT", "11435")
    repo = env("LLAMA_MODEL_REPO", "ggml-org/gemma-4-E4B-it-GGUF")
    stall_sec = int(env("LLAMA_DOWNLOAD_STALL_SECONDS", "600"))
    sb_api = env("SB_API_URL", "http://localhost:3000")
    sb_token = env("SB_API_TOKEN", "")
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    models_dir = os.path.join(data_dir, "llama", "models")
    llama_url = f"http://127.0.0.1:{port}"

    _gpu = env("LLAMA_GPU_PASSTHROUGH", "").strip().lower()
    if _gpu in ("yes", "true", "1"):
        gpu_requested = True
    elif _gpu in ("no", "false", "0", "off"):
        gpu_requested = False
    else:
        gpu_requested = os.path.exists("/etc/cdi/nvidia.yaml")

    try:
        os.makedirs(models_dir, exist_ok=True)
    except OSError as e:
        jlog(
            "error",
            "llama:models",
            "could not create the model directory",
            path=models_dir,
            error=str(e),
        )
        return 0

    # Weights first: the container crash-loops until they exist, and the GPU
    # fixup below restarts it once — so a first install converges without
    # anyone waiting on a restart loop.
    wanted = [
        env("LLAMA_MODEL_FILE", "gemma-4-E4B-it-Q4_0.gguf"),
        env("LLAMA_DRAFT_FILE", "mtp-gemma-4-E4B-it-Q8_0.gguf"),
        env("LLAMA_MMPROJ_FILE", ""),
    ]
    for filename in [f for f in wanted if f]:
        if not download_model(repo, filename, models_dir, stall_sec):
            jlog(
                "warn",
                "llama:models",
                "model file missing — llama-server will not start until it is there. Download it manually into %s from https://huggingface.co/%s"
                % (models_dir, repo),
                file=filename,
            )

    # A deploy in the middle of a lease must not take the card back: rewriting
    # the Quadlet would restart llama-server into a card foundry or the coding
    # run is using, and then wait 15 minutes for a /health that cannot come.
    leased = os.path.exists(lease_file(data_dir))

    if leased:
        jlog(
            "info",
            "llama:bootstrap",
            "a GPU lease is held; leaving llama.service exactly as the lease set it",
            holder=str(read_lease(data_dir).get("holder", "")),
        )
    elif gpu_requested:
        install_gpu_quadlet_fallback(port, data_dir)
    else:
        jlog(
            "info",
            "llama:bootstrap",
            "GPU passthrough not requested; llama-server runs on the CPU and will be slow",
        )

    # Before the wait, not after: a first install that is still loading weights
    # must not be the reason the lease script is missing when foundry asks.
    lease_script = install_lease_script(data_dir)
    save_household_profile(data_dir)

    if leased:
        print("✅ llama-server is under a GPU lease; nothing was changed.")
        print(f"   GPU lease: python3 {lease_script} release")
        return 0

    jlog(
        "info",
        "llama:bootstrap",
        "waiting for llama-server",
        url=llama_url,
        deadline_sec=min(stall_sec, 900),
    )
    if not wait_for_ready(llama_url, deadline_sec=min(stall_sec, 900)):
        jlog(
            "warn",
            "llama:bootstrap",
            "llama-server did not answer /health. Check `journalctl --user -u llama.service` — a missing or truncated GGUF is the usual cause.",
            url=llama_url,
        )
        return 0

    if speculative_active(llama_url):
        jlog("info", "llama:bootstrap", "speculative decoding active (MTP drafter)")
    else:
        jlog(
            "warn",
            "llama:bootstrap",
            "llama-server is up but /slots reports no speculative decoding — the drafter is not in play and answers will take about twice as long. Check LLAMA_DRAFT_FILE.",
        )

    register_http_check(sb_api, sb_token, llama_url)

    print(f"✅ llama-server is running on 127.0.0.1:{port}.")
    print(f"   Models in {models_dir} (from https://huggingface.co/{repo}).")
    print("   The Solaris Engine reaches it via LLAMA_SERVER_URL.")
    if lease_script:
        print(f"   GPU lease: python3 {lease_script} acquire <name> | release")
        print(
            f"   Coding window: python3 {lease_script} acquire coding "
            "--model coding --duration 4h"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
