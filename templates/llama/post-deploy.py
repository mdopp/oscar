#!/usr/bin/env python3
"""
post-deploy hook for the `llama` template.

Three responsibilities:

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

Idempotent: a second run finds the files on disk and skips the download; the
Quadlet is re-activated only when it isn't the live unit source; the
health-check API does upsert-by-id.

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


def server_args(port: str, models_dir_in_container: str) -> list[str]:
    """The llama-server argv, shared by the Quadlet render and the log line.

    Mirrors template.yml's `args`. `--spec-type draft-mtp` is mandatory for
    the MTP drafter and `--draft-max` no longer exists — the current image
    refuses to start on it ("the argument has been removed").
    """
    model_file = env("LLAMA_MODEL_FILE", "gemma-4-E4B-it-Q4_0.gguf")
    draft_file = env("LLAMA_DRAFT_FILE", "mtp-gemma-4-E4B-it-Q8_0.gguf")
    mmproj_file = env("LLAMA_MMPROJ_FILE", "")
    context_length = env("LLAMA_CONTEXT_LENGTH", "32768")
    draft_n_max = env("LLAMA_DRAFT_N_MAX", "4")
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


def render_gpu_container_unit(port: str, data_dir: str) -> str:
    """Render the `.container` Quadlet text for the GPU fixup. Pure, so the
    needs-rewrite comparison and the write share one source of truth."""
    exec_args = " ".join(server_args(port, "/models"))
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

    if gpu_requested:
        install_gpu_quadlet_fallback(port, data_dir)
    else:
        jlog(
            "info",
            "llama:bootstrap",
            "GPU passthrough not requested; llama-server runs on the CPU and will be slow",
        )

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
