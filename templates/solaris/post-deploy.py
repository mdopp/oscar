#!/usr/bin/env python3
"""post-deploy hook for the `solaris` template — the Solaris Engine era.

The Hermes-era 2,800-line sequence (gateway config writer, profile
provisioning, boot hooks, cron registration, MCP block splicing, bundled-
skill opt-outs) is gone: the engine owns all of that in-process. What
remains is the wiring only a deploy can do:

  1. engine soul   — seed/sync SOUL.md on the chat-owned solaris-data volume
                     (#283 guard: an operator-edited soul is never clobbered).
  2. HA            — adopt the long-lived token (#1002, patches the pod yml);
                     auto-install the jellyfin integration (#195); wire the
                     VOICE PIPELINE: wyoming whisper + piper config entries,
                     the ollama-integration conversation agent pointing at the
                     engine's /ollama facade, the "Solaris" Assist pipeline
                     (create via the websocket storage API), set it preferred
                     and assign it to the Voice PE's pipeline select.
  3. admin MCP     — mint the servicebay_admin token (read+lifecycle+mutate,
                     no destroy/exec) and drop it at
                     <DATA_DIR>/solarisbay/sb-admin-token for the engine's admin
                     toolbox (read lazily — no restart needed); AND land the
                     non-expiring read-only token at
                     <DATA_DIR>/solarisbay/sb-read-token so the unattended
                     pollers don't 401-churn on the rotating admin token (#818).
                     ServiceBay mints that read token itself and injects it as
                     the SB_READ_TOKEN env var (servicebay#2317) — the
                     post-deploy just persists it to the file.
  4. ONE restart   — POST /api/services/solaris/action {restart} as the LAST
                     step. Risk-2-safe (#271 spike): ServiceBay runs this
                     script in an SSH session and the restart is `--no-block`
                     async, so it does not kill the running post-deploy.

Every HA step is idempotent + fail-soft: a re-deploy converges, a missing
HA token skips the HA phase with a log line instead of failing the install.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import secrets as _secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

# A ServiceBay-minted MCP token is `sb_<8-hex-id>_<base32-ish-secret>`. Only
# this shape is accepted by ServiceBay's `/mcp` `verifyToken`; any other value
# is a permanent 401 (#126).
SB_MCP_TOKEN_RE = re.compile(r"^sb_[0-9a-f]{8}_[A-Z2-9]+$")

SOLARIS_SERVICE = "solaris"
CHAT_CONTAINER = os.environ.get("CHAT_CONTAINER", "solaris-chat")
GATEKEEPER_CONTAINER = os.environ.get("GATEKEEPER_CONTAINER", "solaris-gatekeeper")

ADMIN_TOKEN_NAME = "admin-soul"
ADMIN_MCP_SCOPES = ["read", "lifecycle", "mutate"]

# Jellyfin service-user credential converge (#626). The engine authenticates to
# Jellyfin as the read-only `solaris` lldap user via JELLYFIN_PASSWORD, but that
# var is a `noAutoGenerate` secret absent from SB installedSecrets, so every
# template render zeroes it → AuthenticateByName 401 → music down. The
# post-deploy owns a persisted password and converges it on both sides.
LLDAP_CONTAINER = os.environ.get("LLDAP_CONTAINER", "auth-lldap")
LLDAP_ADMIN_USER = "admin"
LLDAP_PORT = 17170
JELLYFIN_SOLARIS_USER = "solaris"

# The Radicale ROOT the per-resident calendar/contacts sync PUTs under (#997 /
# #1010 / #1011, option A: `{base}/{resident_uid}/solaris/`). The engine pod is
# host-networked, so Radicale is the box-local loopback root.
# DEADLINES_SYNC_URL_BASE overrides it.
DEADLINES_SYNC_URL_BASE_DEFAULT = "http://127.0.0.1:5232/"

# ── Radicale rights converge (option A, #997 / #1011) ────────────────────────
# The per-resident calendar lives under the RESIDENT's OWN principal
# (`/<resident>/solaris/`) so the resident subscribes as themselves — no shared
# password. Radicale ships `[rights] type = owner_only`, which lets a principal
# touch ONLY its own tree, so the `solaris` DAV account gets 403 writing
# `/<resident>/solaris/`. We converge Radicale's rights to an equivalent
# `from_file` ruleset that keeps owner_only's guarantee AND grants `solaris`
# write access to `<resident>/solaris` (and nothing else). Radicale is a
# ServiceBay-registry service, so its pod manifest is regenerated (back to
# owner_only) on a radicale re-render; this re-applies on the next solaris
# deploy — the same converge model as the Jellyfin credential. `solaris` is a
# dependency-ordered install AFTER radicale, so on a fresh box this runs once
# radicale exists.
RADICALE_POD_YML = "~/.config/containers/systemd/radicale.yml"
RADICALE_RIGHTS_FILE = "/config/rights"
# The from_file body. Radicale substitutes `{0}` in a `collection` pattern with
# the FIRST CAPTURING GROUP of that section's `user` regex (it calls
# `pattern.format(*user_match.groups())`) — so the owner rule's user regex MUST
# capture (`(.+)`, not a bare `.+`, which has no group 0 and raises IndexError →
# every request 500s). Uppercase RrWw = full read/write on the collection AND its
# children (so MKCALENDAR + PUT both pass).
_RADICALE_RIGHTS_BODY = [
    "# Owner-only base: each authenticated user has full read/write over their",
    "# OWN principal subtree — and nothing else (equivalent to owner_only).",
    "[owner]",
    "user: (.+)",
    "collection: {0}(/.*)?",
    "permissions: RrWw",
    "",
    "# The `solaris` service account may ADDITIONALLY read/write ONLY the",
    "# `solaris` calendar under ANY resident principal — the per-resident",
    "# calendar the deadlines/tasks sync writes (option A, #997/#1011). No",
    "# reach into a resident's other calendars nor the principal root.",
    "[solaris-subcal]",
    "user: solaris",
    "collection: [^/]+/solaris(/.*)?",
    "permissions: RrWw",
]

PIPELINE_NAME = "Solaris"
CONVERSATION_AGENT_NAME = "Solaris"
ENGINE_MODEL = "solaris"
# The single-word wake word (#407), no Hey/Ok prefix. The trained .tflite is
# produced offline by scripts/train-wake-word.py and installed into the voice
# service's custom-model dir; the openWakeWord wyoming integration exposes it
# as a wake_word with this id.
WAKE_WORD_MODEL = "solaris"
OPENWAKEWORD_CUSTOM_DIR = "/mnt/data/voice/custom"

# ── Solaris-owned voice pipeline (#456) ──────────────────────────────────────
# Solaris owns its WHOLE voice pipeline (wake word "Solaris", Whisper STT, the
# Kokoro "martin" voice + bridge) instead of it living in ServiceBay's
# app-agnostic `voice` template. Whisper and the Kokoro TTS need the GPU, and
# `podman kube play` silently drops CDI device requests when expressed as
# resources.limits (#1026) — so the GPU units are companion `.container`
# Quadlets with `AddDevice=nvidia.com/gpu=all` + `SecurityLabelDisable=true`,
# exactly the ollama-fixup pattern. openWakeWord rides the solaris pod
# (template.yml) because it needs no GPU. ServiceBay provides only the platform
# capability (GPU/CDI passthrough, HA, the Wyoming runtime/images).
# GPU voice services are companion Quadlets (CDI is dropped inside kube-play
# pods, #1026), named solaris-* so they group under the Solaris namespace. The
# CPU services — openWakeWord + the TTS bridge — ride the solaris pod itself
# (see template.yml), so they have no constants here.
WHISPER_UNIT = "solaris-whisper"
TTS_UNIT = "solaris-tts"
TTS_IMAGE = "ghcr.io/mdopp/solaris-tts:latest"

# The whisper wizard default. On a CDI GPU box the default upgrades to the
# better model the GPU runs faster than the CPU ran base (box-measured 0.38s vs
# 0.83-2.86s, servicebay#1809). An explicit non-default model is kept on both
# paths (one knob, no GPU-specific knob).
WHISPER_CPU_DEFAULT_MODEL = "base-int8"
WHISPER_GPU_DEFAULT_MODEL = "medium-int8"
# HA's conversation subentry prompt — folded after the engine's own system
# block by the facade, so keep it to the voice-delivery essentials.
VOICE_PROMPT = "Antworte kurz, gesprochen und ohne Markdown."

HA_URL = "http://127.0.0.1:8123"


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val if val else default


def _truthy(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes", "on"}


def jlog(level: str, tag: str, message: str, **args: object) -> None:
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


def emit_credential(**fields: object) -> None:
    sys.stdout.write("__SB_CREDENTIAL__ " + json.dumps(fields) + "\n")
    sys.stdout.flush()


def post_json(
    url: str, payload: dict[str, object], timeout: float = 10.0
) -> tuple[int, dict[str, object] | None]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SB_API_TOKEN", "")
    if token:
        headers["X-SB-Internal-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(data) if data else None
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def get_json(url: str, timeout: float = 10.0) -> tuple[int, dict[str, object] | None]:
    headers = {}
    token = os.environ.get("SB_API_TOKEN", "")
    if token:
        headers["X-SB-Internal-Token"] = token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(data) if data else None
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def _container_env(container: str, name: str) -> str:
    """Read an env var from inside a running pod container — the rendered
    template value. The post-deploy runs in ServiceBay's context, which does
    NOT export the template variables to it, so the container is the source
    of truth. Returns '' if the container or var is unavailable."""
    try:
        proc = subprocess.run(
            ["podman", "exec", container, "printenv", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def chat_container_env(name: str) -> str:
    return _container_env(CHAT_CONTAINER, name)


def gatekeeper_container_env(name: str) -> str:
    return _container_env(GATEKEEPER_CONTAINER, name)


def selected_tts_voice(default: str = "martin") -> str:
    """The admin-selected global Kokoro voice (#368), persisted in the chat
    container's app_settings.json beside solaris.db. Read it from inside the
    container (the rendered DB path + the chat-owned volume). Empty/unset ⇒ the
    Martin default, so an install that never touched the picker is unchanged."""
    db_path = chat_container_env("SOLARIS_DB_PATH") or "/var/lib/solaris/solaris.db"
    settings_path = f"{os.path.dirname(db_path)}/app_settings.json"
    try:
        proc = subprocess.run(
            ["podman", "exec", CHAT_CONTAINER, "cat", settings_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    if proc.returncode != 0:
        return default
    try:
        voice = json.loads(proc.stdout).get("tts_voice")
    except (ValueError, AttributeError):
        return default
    return voice.strip() if isinstance(voice, str) and voice.strip() else default


# ════════════════════════════════════════════════════════════════════════════
# 0. VOICE PIPELINE QUADLETS — Solaris owns whisper + Kokoro-Martin TTS +
#    the wyoming bridge end-to-end (#456). GPU units via CDI companion Quadlets
#    (#1026); openWakeWord rides the solaris pod. Ported from ServiceBay's
#    voice template (servicebay#1809/#1815/#1832) — same Wyoming endpoints, so
#    the Assist-pipeline wiring downstream is unchanged.
# ════════════════════════════════════════════════════════════════════════════


def cdi_available() -> bool:
    """True when an NVIDIA CDI spec is registered (#1026) — the marker the
    ollama fixup and the voice template both use to choose the GPU path."""
    return os.path.exists("/etc/cdi/nvidia.yaml")


def service_active(unit: str) -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", f"{unit}.service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
    return out.stdout.strip() == "active"


def install_unit(unit: str, content: str) -> bool:
    """Write + activate one companion `.container` Quadlet, idempotently:
    rewrite only on content drift; (re)start when drifted or inactive."""
    systemd_dir = os.path.expanduser("~/.config/containers/systemd")
    unit_path = os.path.join(systemd_dir, f"{unit}.container")
    existing = ""
    if os.path.exists(unit_path):
        try:
            with open(unit_path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = ""
    if existing == content and service_active(unit):
        jlog("info", "voice-unit", f"{unit}: current and active — no-op")
        return True
    try:
        os.makedirs(systemd_dir, exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(unit_path, 0o644)
    except OSError as e:
        jlog("warn", "voice-unit", f"{unit}: could not write unit", error=str(e))
        return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    started = subprocess.run(
        ["systemctl", "--user", "restart", f"{unit}.service"],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "warn",
            "voice-unit",
            f"{unit}: systemctl restart failed",
            error=started.stderr[:300],
        )
        return False
    jlog("info", "voice-unit", f"{unit}: installed + started")
    return True


# STT health probe (#610). A wedged CUDA context (cudaErrorInvalidDevice)
# leaves the whisper container "Up" while every transcription fails — the
# process never exits, so Restart= never fires and nothing exercises STT, so
# the box shows green while voice is dead. This probe runs a real ~1s Wyoming
# transcription against the local listener and exits non-zero if no transcript
# comes back; the unit's HealthCmd runs it and HealthOnFailure=kill kills the
# container on repeated failure so systemd restarts it fresh (re-injecting
# CDI). Stdlib-only so it runs unchanged in either whisper image.
STT_HEALTHCHECK = """import socket, json, struct, math, sys, time

HOST, PORT = "127.0.0.1", 10300
_NL = chr(10)


def _send(sock, etype, data=None, payload=None):
    hdr = {"type": etype}
    if data is not None:
        hdr["data"] = data
    if payload is not None:
        hdr["payload_length"] = len(payload)
    sock.sendall((json.dumps(hdr) + _NL).encode("utf-8"))
    if payload is not None:
        sock.sendall(payload)


def main():
    rate = 16000
    pcm = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / rate)))
        for i in range(rate)
    )
    try:
        sock = socket.create_connection((HOST, PORT), timeout=10)
    except OSError:
        return 1
    sock.settimeout(30)
    try:
        _send(sock, "transcribe", {"language": "de"})
        _send(
            sock,
            "audio-start",
            {"rate": rate, "width": 2, "channels": 1, "timestamp": 0},
        )
        for off in range(0, len(pcm), 4000):
            _send(
                sock,
                "audio-chunk",
                {"rate": rate, "width": 2, "channels": 1, "timestamp": off},
                pcm[off : off + 4000],
            )
        _send(sock, "audio-stop", {"timestamp": 1000})
        buf = b""
        nl = bytes([10])
        deadline = time.time() + 30
        while time.time() < deadline:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            while nl in buf:
                line, buf = buf.split(nl, 1)
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if evt.get("type") == "transcript":
                    return 0
        return 1
    except OSError:
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
"""


def render_whisper_unit(data_dir: str, model: str, language: str, gpu: bool) -> str:
    """Render the voice-whisper `.container` Quadlet (pure). GPU path uses the
    linuxserver faster-whisper:gpu image with the CDI device + SELinux
    relaxation (#1026); CPU path the rhasspy wyoming-whisper image. Same
    Wyoming endpoint (tcp://0.0.0.0:10300) either way."""
    if gpu:
        return (
            "[Unit]\n"
            "Description=Solaris Voice Whisper STT (Wyoming, GPU via CDI #456)\n"
            "Wants=network-online.target\n"
            "After=network-online.target\n"
            "\n"
            "[Container]\n"
            "Image=lscr.io/linuxserver/faster-whisper:gpu\n"
            f"ContainerName={WHISPER_UNIT}\n"
            "Network=host\n"
            f"Environment=WHISPER_MODEL={model}\n"
            f"Environment=WHISPER_LANG={language}\n"
            "# Beam 1: greedy decode — GPU headroom goes into the bigger model.\n"
            "Environment=WHISPER_BEAM=1\n"
            "# CDI device — podman kube play silently drops this when expressed\n"
            "# as resources.limits (#1026), which is why whisper is a Quadlet.\n"
            "AddDevice=nvidia.com/gpu=all\n"
            "# SELinux relaxation for NVML/CUDA init on FCoS (same fixup as\n"
            "# ollama, #1026) — without it driver init fails.\n"
            "SecurityLabelDisable=true\n"
            "# linuxserver image keeps its model cache under /config.\n"
            f"Volume={data_dir}/voice/whisper-gpu:/config:Z\n"
            f"Volume={data_dir}/voice/stt_healthcheck.py:/stt_healthcheck.py:ro,Z\n"
            "AutoUpdate=registry\n"
            "# STT health probe (#610): a wedged CUDA context leaves the\n"
            "# container Up while every transcription throws invalid-device, so\n"
            "# Restart= never fires. Run a real Wyoming transcription; on\n"
            "# repeated failure kill the container so systemd restarts it fresh\n"
            "# (re-injecting CDI).\n"
            "HealthCmd=python3 /stt_healthcheck.py\n"
            "HealthInterval=3m\n"
            "HealthTimeout=40s\n"
            "HealthStartPeriod=5m\n"
            "HealthRetries=3\n"
            "HealthOnFailure=kill\n"
            "\n"
            "[Service]\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    return (
        "[Unit]\n"
        "Description=Solaris Voice Whisper STT (Wyoming, CPU #456)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "Image=docker.io/rhasspy/wyoming-whisper:latest\n"
        f"ContainerName={WHISPER_UNIT}\n"
        "Network=host\n"
        f"Exec=--model {model} --language {language}"
        " --data-dir /data --uri tcp://0.0.0.0:10300\n"
        f"Volume={data_dir}/voice/whisper:/data:Z\n"
        f"Volume={data_dir}/voice/stt_healthcheck.py:/stt_healthcheck.py:ro,Z\n"
        "AutoUpdate=registry\n"
        "# STT health probe (#610): restart whisper when a transcription\n"
        "# round-trip fails (mirrors the GPU path).\n"
        "HealthCmd=python3 /stt_healthcheck.py\n"
        "HealthInterval=3m\n"
        "HealthTimeout=40s\n"
        "HealthStartPeriod=5m\n"
        "HealthRetries=3\n"
        "HealthOnFailure=kill\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_tts_unit() -> str:
    """Render the Kokoro-Martin TTS `.container` Quadlet (pure, GPU via CDI).
    The bundled solaris-tts image serves the OpenAI-compatible API on :8881."""
    return (
        "[Unit]\n"
        "Description=Solaris Voice TTS Kokoro-Martin (OpenAI API, GPU via CDI #456)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        f"Image={TTS_IMAGE}\n"
        f"ContainerName={TTS_UNIT}\n"
        "Network=host\n"
        "# The 82M ONNX model on the CUDA provider: box-measured 0.29-0.36s\n"
        "# for a 7.4s sentence, 0.03s warm for a short one, ~1.2 GiB VRAM.\n"
        "Environment=KOKORO_ONNX_PROVIDER=cuda\n"
        "Environment=KOKORO_ONNX_VOICE=martin\n"
        "Environment=KOKORO_ONNX_LANG=de\n"
        "AddDevice=nvidia.com/gpu=all\n"
        "SecurityLabelDisable=true\n"
        "AutoUpdate=registry\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_whisper_unit(data_dir: str) -> bool:
    """Write + activate the companion whisper Quadlet (GPU when CDI is
    registered, CPU otherwise). Creates the model-cache host dir first —
    Quadlet Volume= does NOT create it (unlike kube DirectoryOrCreate), so
    without this the unit fails `statfs …: no such file or directory`."""
    gpu = cdi_available()
    model = env("WHISPER_MODEL", WHISPER_CPU_DEFAULT_MODEL)
    if gpu and model == WHISPER_CPU_DEFAULT_MODEL:
        model = WHISPER_GPU_DEFAULT_MODEL
    language = env("WHISPER_LANGUAGE", "de")
    volume_dir = os.path.join(data_dir, "voice", "whisper-gpu" if gpu else "whisper")
    try:
        os.makedirs(volume_dir, exist_ok=True)
    except OSError as e:
        jlog("warn", "voice-unit", "whisper: could not create cache dir", error=str(e))
        return False
    # Drop the STT health probe next to the cache dir; the Quadlet mounts it
    # read-only and runs it as HealthCmd (#610). Best-effort — a missing probe
    # would just leave the container without its self-heal, never block install.
    probe_path = os.path.join(data_dir, "voice", "stt_healthcheck.py")
    try:
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write(STT_HEALTHCHECK)
        os.chmod(probe_path, 0o644)
    except OSError as e:
        jlog("warn", "voice-unit", "whisper: could not write STT probe", error=str(e))
    jlog(
        "info",
        "voice-unit",
        "whisper path selected",
        gpu=gpu,
        model=model,
    )
    return install_unit(
        WHISPER_UNIT, render_whisper_unit(data_dir, model, language, gpu)
    )


def install_tts_units() -> bool:
    """GPU boxes get Solaris's Martin voice: the Kokoro OpenAI TTS on :8881, a
    GPU companion Quadlet. The wyoming bridge that fronts it as an HA TTS entity
    (:10203) is a CPU container in the solaris pod (template.yml), not here.
    CPU-only boxes have no Kokoro TTS (the bundled image needs CUDA); the Assist
    wiring then keeps piper. Returns True when the GPU TTS unit is up."""
    if not cdi_available():
        jlog("info", "voice-unit", "tts: no CDI GPU — skipping Kokoro-Martin unit")
        return False
    return install_unit(TTS_UNIT, render_tts_unit())


def setup_custom_models_dir(custom_dir: str) -> None:
    """Ensure the openWakeWord custom-models host dir exists (#456 folds in
    #407). template.yml mounts it into the openwakeword pod container at
    /custom_models and passes `--custom-model-dir /custom_models`; the trained
    "Solaris" `.tflite` is dropped here by install_wake_word_model. Empty/unset
    custom_dir → no-op."""
    if not custom_dir:
        return
    try:
        os.makedirs(custom_dir, exist_ok=True)
    except OSError as e:
        jlog(
            "warn",
            "wakeword",
            "could not create custom models dir",
            path=custom_dir,
            error=str(e),
        )
        return
    jlog("info", "wakeword", "custom models dir ready", path=custom_dir)


def install_voice_pipeline(data_dir: str) -> None:
    """Stand up the Solaris-owned voice pipeline (#456). The GPU services are
    companion Quadlets (CDI is dropped in kube-play pods, #1026): whisper STT
    and the Kokoro-Martin TTS. The CPU services — openWakeWord and the TTS
    bridge — ride the solaris pod itself (template.yml). Here we install the
    GPU Quadlets and drop the trained wake-word model into the custom-models
    dir the pod's openWakeWord container mounts. The Assist-pipeline wiring
    (later, in wire_voice_pipeline) points HA at these Wyoming endpoints.

    The whisper model cache lives at the same host path
    (<data_dir>/voice/whisper{-gpu}) the ServiceBay voice template wrote, so
    the multi-gigabyte cache is reused in place across the ownership move — no
    data migration."""
    custom_dir = env("OPENWAKEWORD_CUSTOM_DIR", OPENWAKEWORD_CUSTOM_DIR)
    setup_custom_models_dir(custom_dir)
    # Drop the trained model into the custom dir the pod's openWakeWord mounts.
    install_wake_word_model(
        data_dir, custom_dir, env("WAKE_WORD_MODEL", WAKE_WORD_MODEL)
    )
    install_whisper_unit(data_dir)
    install_tts_units()


# ════════════════════════════════════════════════════════════════════════════
# 1. ENGINE SOUL — seed/sync SOUL.md on the chat-owned volume.
# ════════════════════════════════════════════════════════════════════════════


def _soul_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_engine_soul(data_dir: str) -> bool:
    """Seed/sync the Solaris Engine's SOUL.md on the chat-owned solarisbay volume.

    The engine reads the soul from `<data_dir>/solarisbay/SOUL.md` and the panel
    writes it directly. #283 guard: an operator-edited file is never
    clobbered; an unmodified shipped soul is updated when the pack ships a
    new one. Pure host-side file IO. Returns True when the file was written."""
    source = os.path.join(data_dir, "solaris", "skills", "household", "SOUL.md")
    target = os.path.join(data_dir, "solarisbay", "SOUL.md")
    marker = os.path.join(data_dir, "solarisbay", ".soul.shipped.sha256")
    try:
        with open(source, encoding="utf-8") as f:
            soul = f.read()
    except OSError:
        jlog("warn", "soul", "shipped SOUL.md not readable", source=source)
        return False
    existing = ""
    try:
        with open(target, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        pass
    if existing == soul:
        return False
    recorded = ""
    try:
        with open(marker, encoding="utf-8") as f:
            recorded = f.read().strip()
    except OSError:
        pass
    if existing.strip() and recorded and recorded != _soul_sha256(existing):
        jlog("info", "soul", "leaving operator-edited SOUL.md untouched", path=target)
        return False
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(soul)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(_soul_sha256(soul) + "\n")
    except OSError as e:
        jlog("error", "soul", "could not write engine SOUL.md", error=str(e))
        return False
    jlog("info", "soul", "installed engine SOUL.md", path=target)
    return True


def ship_alarm_sound(data_dir: str) -> bool:
    """Copy the shipped default alarm tone into HA's media folder so
    `media-source://media_source/local/solaris-alarm.ogg` resolves and the
    Voice PE can ring it (#348). Host-side file IO; idempotent (skips when the
    target already matches). An operator's own tone in the media folder is
    never touched — only `solaris-alarm.ogg` is managed. Returns True when
    the file was written."""
    source = os.path.join(
        data_dir, "solaris", "skills", "household", "media", "solaris-alarm.ogg"
    )
    media_dir = os.path.join(data_dir, "home-assistant", "homeassistant", "media")
    target = os.path.join(media_dir, "solaris-alarm.ogg")
    try:
        with open(source, "rb") as f:
            tone = f.read()
    except OSError:
        jlog("warn", "alarm", "shipped alarm tone not readable", source=source)
        return False
    try:
        with open(target, "rb") as f:
            if f.read() == tone:
                return False
    except OSError:
        pass
    try:
        os.makedirs(media_dir, exist_ok=True)
        with open(target, "wb") as f:
            f.write(tone)
    except OSError as e:
        jlog("error", "alarm", "could not install alarm tone", error=str(e))
        return False
    jlog("info", "alarm", "installed default alarm tone", path=target)
    return True


# ════════════════════════════════════════════════════════════════════════════
# 2. HOME ASSISTANT — token adoption, jellyfin, the voice pipeline.
# ════════════════════════════════════════════════════════════════════════════


def _ha_token_timeout() -> int:
    # On a fresh install HA's auto-onboarding (servicebay#1847) writes
    # `.solaris-long-lived-token` only at the END of HA's first boot+onboard
    # run, which on a cold box runs past the old 90s window — the deploy then
    # adopted nothing and the engine came up with an empty HASS_TOKEN (#425).
    # 180s covers a cold first boot; an existing token is found on the first
    # poll so a re-deploy doesn't pay this.
    return int(os.environ.get("HA_TOKEN_TIMEOUT", "180"))


def _ha_api_timeout() -> int:
    return int(os.environ.get("HA_API_TIMEOUT", "60"))


def _wait_for_ha_token(token_path: str, deadline_secs: int | None = None) -> str | None:
    """#1002 — Poll for the HA long-lived token file HA's post-deploy writes
    near the end of its run. Returns the token once present + non-empty, or
    None at the deadline (0 = check once)."""
    if deadline_secs is None:
        deadline_secs = _ha_token_timeout()
    deadline = time.time() + deadline_secs
    while True:
        if os.path.exists(token_path):
            try:
                with open(token_path, encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    return token
            except OSError:
                pass
        if time.time() >= deadline:
            return None
        time.sleep(3)


def _wait_for_ha_api(token: str, timeout_secs: int | None = None) -> bool:
    """Probe HA's /api/ with the token until it answers 200 (best-effort)."""
    if timeout_secs is None:
        timeout_secs = _ha_api_timeout()
    if timeout_secs <= 0:
        return False
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        status, _ = _ha_get("/api/", token, timeout=5)
        if 200 <= status < 300:
            return True
        time.sleep(3)
    jlog("warn", "ha", "HA /api/ not 200 within deadline", deadline_secs=timeout_secs)
    return False


def adopt_ha_long_lived_token(data_dir: str) -> str | None:
    """Pick up HA's auto-onboarded long-lived token (#934/#1002) and patch the
    deployed `solaris.yml` pod manifest's HASS_TOKEN env value so the engine
    can authenticate. Returns the token, or None when the file never appears."""
    token_path = os.path.join(
        data_dir, "home-assistant", "homeassistant", ".solaris-long-lived-token"
    )
    token = _wait_for_ha_token(token_path)
    if token is None:
        jlog(
            "warn",
            "ha",
            "no HA long-lived token to adopt — HASS_TOKEN stays empty, the engine "
            "cannot reach Home Assistant (no device control / Assist tool calls). "
            "ServiceBay's HA auto-onboarding (#934/#1002) writes this file; a fresh "
            "install with no migrated data won't have it until HA onboarding has run. "
            "Fix: (re)run HA auto-onboarding so it writes the token file, or drop a "
            "long-lived token there by hand, then redeploy Solaris to adopt it.",
            path=token_path,
        )
        return None
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "ha", "solaris.yml not found at expected path", path=pod_yml)
        return None
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "ha", "could not read solaris.yml", path=pod_yml, error=str(e))
        return None
    new, n = re.subn(
        r'(- name: HASS_TOKEN\n\s+value: )(?:"[^"\n]*"|[^\n]*)',
        lambda m: m.group(1) + '"' + token + '"',
        src,
    )
    if n == 0:
        # The regex found no HASS_TOKEN env line to patch — adoption would
        # silently no-op and the engine would restart with an empty token. Fail
        # loudly instead of returning a token that never reaches the container.
        jlog(
            "warn",
            "ha",
            "HASS_TOKEN env entry not found in solaris.yml — token NOT adopted; "
            "the engine cannot reach Home Assistant. Check the chat container's "
            "HASS_TOKEN env block in templates/solaris/template.yml.",
            path=pod_yml,
        )
        return None
    if new != src:
        try:
            with open(pod_yml, "w", encoding="utf-8") as f:
                f.write(new)
        except OSError as e:
            jlog("warn", "ha", "could not write patched solaris.yml", error=str(e))
            return None
        jlog("info", "ha", "adopted HA long-lived token", token_path=token_path)
    else:
        jlog("info", "ha", "HASS_TOKEN already current in solaris.yml")
    _wait_for_ha_api(token)
    return token


def _patched_cast_url_yaml(src: str, cast_url: str) -> tuple[str, int]:
    """Stamp the JELLYFIN_CAST_URL env value in the pod manifest text (pure).

    Returns (new_text, n_replacements). Same `- name: …\\n value: …` patch
    shape adopt_ha_long_lived_token uses for HASS_TOKEN."""
    return re.subn(
        r'(- name: JELLYFIN_CAST_URL\n\s+value: )(?:"[^"\n]*"|[^\n]*)',
        lambda m: m.group(1) + '"' + cast_url + '"',
        src,
    )


def stamp_jellyfin_cast_url(data_dir: str) -> str | None:
    """Derive JELLYFIN_CAST_URL from the box LAN IP when it's left empty (#607).

    Casting a Jellyfin track to a Chromecast needs a base the device can reach
    on the LAN, not the engine's loopback JELLYFIN_URL. ServiceBay exposes the
    box LAN IP to the post-deploy as the LAN_IP env var, so an empty
    JELLYFIN_CAST_URL is stamped to `http://<lanIp>:8096` in the deployed
    solaris.yml (the restart at the end of main() picks it up) — durable on
    reinstall, no hardcoded IP, no operator knob. Already-set value is left as
    is; an absent LAN_IP leaves it empty (the engine config falls back to
    JELLYFIN_URL). Returns the stamped URL, or None when nothing was changed."""
    if env("JELLYFIN_CAST_URL").strip():
        return None
    lan_ip = env("LAN_IP").strip()
    if not lan_ip:
        jlog(
            "info",
            "jellyfin",
            "no LAN_IP — JELLYFIN_CAST_URL stays empty (engine falls back to "
            "JELLYFIN_URL)",
        )
        return None
    cast_url = f"http://{lan_ip}:8096"
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "jellyfin", "solaris.yml not found at expected path", path=pod_yml)
        return None
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "jellyfin", "could not read solaris.yml", error=str(e))
        return None
    new, n = _patched_cast_url_yaml(src, cast_url)
    if n == 0:
        jlog(
            "warn",
            "jellyfin",
            "JELLYFIN_CAST_URL env entry not found in solaris.yml — not stamped",
            path=pod_yml,
        )
        return None
    if new == src:
        return cast_url
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "jellyfin", "could not write patched solaris.yml", error=str(e))
        return None
    jlog("info", "jellyfin", "stamped JELLYFIN_CAST_URL from LAN_IP", cast_url=cast_url)
    return cast_url


def _ha_get(path: str, token: str, timeout: float = 10.0) -> tuple[int, object]:
    """GET against HA's API with the long-lived token. 0 on connection failure."""
    req = urllib.request.Request(
        f"{HA_URL}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return 0, None


def _ha_post(
    path: str, token: str, payload: dict[str, object], timeout: float = 30.0
) -> tuple[int, object]:
    """POST JSON against HA's API with the long-lived token. 0 on failure."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return 0, None


def _ha_request_delete(path: str, token: str, timeout: float = 10.0) -> None:
    """Best-effort DELETE against HA's API (used to abort a dangling flow)."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return


def ensure_ha_jellyfin_integration(
    token: str, url: str, username: str, password: str
) -> bool:
    """Auto-install HA's `jellyfin` integration via HA's config-entries flow
    API (#195). Idempotent (skips if an entry exists) + fail-soft. Returns
    True only when a new entry was created."""
    if not (token and url and username):
        return False

    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status != 200 or not isinstance(entries, list):
        jlog("warn", "jellyfin", "could not list HA config entries", status=status)
        return False
    if any(isinstance(e, dict) and e.get("domain") == "jellyfin" for e in entries):
        jlog("info", "jellyfin", "HA jellyfin config entry already present")
        return False

    status, flow = _ha_post(
        "/api/config/config_entries/flow", token, {"handler": "jellyfin"}
    )
    if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
        jlog("warn", "jellyfin", "could not start jellyfin config flow", status=status)
        return False
    flow_id = flow["flow_id"]

    status, result = _ha_post(
        f"/api/config/config_entries/flow/{flow_id}",
        token,
        {"url": url, "username": username, "password": password},
    )
    if (
        status == 200
        and isinstance(result, dict)
        and result.get("type") == "create_entry"
    ):
        jlog("info", "jellyfin", "created HA jellyfin config entry", url=url)
        return True

    errors = result.get("errors") if isinstance(result, dict) else None
    _ha_request_delete(f"/api/config/config_entries/flow/{flow_id}", token)
    jlog("warn", "jellyfin", "jellyfin flow did not create an entry", errors=errors)
    return False


# ── Jellyfin service-user credential converge (#626) ─────────────────────────
# JELLYFIN_PASSWORD is a `noAutoGenerate` secret that SB never stored, so every
# `sb stacks install`/template render writes it back to "" → the engine's
# AuthenticateByName as the read-only `solaris` lldap user 401s → music down
# (happened live 2026-06-26). The fix converges the credential on every deploy:
# (1) persist a managed password under DATA_DIR (generate once, reuse after),
# (2) reset the lldap `solaris` user's password to it via the lldap admin (the
#     same `lldap_set_password` path the SSO smoke test uses), and
# (3) patch the deployed solaris.yml pod env JELLYFIN_PASSWORD to it — the
#     restart at the end of main() makes the engine pick it up.
# Same value each deploy ⇒ a no-op after the first; idempotent + best-effort so
# a hiccup never blocks the deploy. Kept out of git/logs (file is 0600; the
# value is never logged).


def _persisted_jellyfin_password(data_dir: str) -> str | None:
    """Read/mint the managed Jellyfin password for the `solaris` service user,
    persisted at <data_dir>/solarisbay/.jellyfin-solaris-password (0600).

    Generates a strong URL-safe value once if absent (so a first deploy seeds
    it), reuses the persisted value afterwards (so every later render reapplies
    the SAME password — no churn). Returns None only when the dir can't be
    written. Never logs the value."""
    path = os.path.join(data_dir, "solarisbay", ".jellyfin-solaris-password")
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    password = _secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(password + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        jlog("warn", "jellyfin", "could not persist Jellyfin password", error=str(e))
        return None
    jlog("info", "jellyfin", "minted managed Jellyfin service-user password")
    return password


def _lldap_admin_token() -> str:
    """Log in to lldap as `admin` (password read from the auth-lldap container
    env, the same source the SSO smoke test uses) and return the JWT, or ''."""
    admin_pass = _container_env(LLDAP_CONTAINER, "LLDAP_LDAP_USER_PASS")
    if not admin_pass:
        jlog("info", "jellyfin", "no lldap admin password — skipping cred reset")
        return ""
    status, body = post_json(
        f"http://127.0.0.1:{LLDAP_PORT}/auth/simple/login",
        {"username": LLDAP_ADMIN_USER, "password": admin_pass},
        timeout=10,
    )
    token = body.get("token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(token, str) or not token:
        jlog("warn", "jellyfin", "lldap admin login failed", status=status)
        return ""
    return token


def reset_lldap_solaris_password(password: str) -> bool:
    """Reset the lldap `solaris` user's password to `password` via the
    in-container `lldap_set_password` binary (the SSO smoke-test path).
    Best-effort — returns False on any miss without raising. The value is
    passed as an argv element and never logged."""
    token = _lldap_admin_token()
    if not token:
        return False
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                LLDAP_CONTAINER,
                "/app/lldap_set_password",
                "-u",
                JELLYFIN_SOLARIS_USER,
                "-p",
                password,
                "--base-url",
                f"http://localhost:{LLDAP_PORT}",
                "--token",
                token,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog("warn", "jellyfin", "lldap_set_password did not run", error=str(e))
        return False
    if proc.returncode != 0:
        jlog(
            "warn",
            "jellyfin",
            "lldap_set_password failed",
            user=JELLYFIN_SOLARIS_USER,
            stderr=proc.stderr[:200],
        )
        return False
    jlog("info", "jellyfin", "reset lldap solaris password", user=JELLYFIN_SOLARIS_USER)
    return True


def _patched_jellyfin_password_yaml(src: str, password: str) -> tuple[str, int]:
    """Stamp the JELLYFIN_PASSWORD env value in the pod manifest text (pure).

    Returns (new_text, n_replacements). Same `- name: …\\n value: …` patch
    shape adopt_ha_long_lived_token uses for HASS_TOKEN."""
    return re.subn(
        r'(- name: JELLYFIN_PASSWORD\n\s+value: )(?:"[^"\n]*"|[^\n]*)',
        lambda m: m.group(1) + '"' + password + '"',
        src,
    )


def apply_jellyfin_password_to_engine(password: str) -> bool:
    """Patch JELLYFIN_PASSWORD into the deployed solaris.yml so the engine reads
    the managed value (the template render zeroed it). The restart at the end of
    main() picks it up. Best-effort. Returns True when the manifest now carries
    the value."""
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "jellyfin", "solaris.yml not found at expected path", path=pod_yml)
        return False
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "jellyfin", "could not read solaris.yml", error=str(e))
        return False
    new, n = _patched_jellyfin_password_yaml(src, password)
    if n == 0:
        jlog(
            "warn",
            "jellyfin",
            "JELLYFIN_PASSWORD env entry not found in solaris.yml — not stamped",
            path=pod_yml,
        )
        return False
    if new == src:
        return True
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "jellyfin", "could not write patched solaris.yml", error=str(e))
        return False
    jlog("info", "jellyfin", "stamped managed JELLYFIN_PASSWORD into solaris.yml")
    return True


def _patch_env_value(src: str, name: str, value: str) -> tuple[str, int]:
    """PATCH a single `- name: NAME\\n value: …` env entry's value in place."""
    return re.subn(
        rf'(- name: {re.escape(name)}\n\s+value: )(?:"[^"\n]*"|[^\n]*)',
        lambda m: m.group(1) + '"' + value + '"',
        src,
    )


def _insert_after_caldav_anchor(src: str, entries: list[tuple[str, str]]) -> str:
    """Insert `entries` (name, value) into the engine env list immediately after
    the `- name: CALDAV_URL\\n <ind>  value: "…"` anchor, reusing the anchor's
    OWN indentation (name-line + value-line indent captured, never assumed).
    Returns src unchanged (with a warn) when the anchor is absent, so a missing
    anchor never corrupts the manifest. Guards the result with a YAML parse — a
    bad insert would crash-loop the pod."""
    m = re.search(
        r'\n(?P<ind>[ \t]*)- name: CALDAV_URL\n(?P<vind>[ \t]*)value: "[^"\n]*"',
        src,
    )
    if not m:
        jlog(
            "warn",
            "sync-dav",
            "CALDAV_URL anchor not found in solaris.yml — SYNC_DAV env NOT "
            "inserted; the DAV sync stays dormant. Check the engine container's "
            "env block in the rendered pod.",
        )
        return src
    name_ind = m.group("ind")
    value_ind = m.group("vind")
    block = "".join(
        f'\n{name_ind}- name: {name}\n{value_ind}value: "{value}"'
        for name, value in entries
    )
    new = src[: m.end()] + block + src[m.end() :]
    # Lazy import: PyYAML is present in the pod image (where this runs) but not in
    # the templates CI env; skip the validity check there rather than fail import.
    try:
        import yaml
    except ImportError:
        yaml = None
    try:
        if yaml is not None:
            yaml.safe_load(new)
    except yaml.YAMLError as e:
        jlog(
            "warn",
            "sync-dav",
            "SYNC_DAV insert produced invalid YAML — abandoning it to avoid a "
            "pod crash-loop",
            error=str(e),
        )
        return src
    return new


def _patched_sync_dav_yaml(src: str, password: str) -> tuple[str, int]:
    """Wire SYNC_DAV_USERNAME=solaris + SYNC_DAV_PASSWORD + DEADLINES_SYNC_URL_BASE
    into the engine container's env list (pure). Returns (new_text, n) with n>0
    when the manifest now carries the values.

    On the box the rendered solaris.yml does NOT contain the SYNC_DAV_* entries
    at all (the template.yml block isn't reaching the rendered pod, #997 / #1010
    option B), so a PATCH-only pass found nothing and the sync stayed dormant.
    So: PATCH the entries in place when SYNC_DAV_PASSWORD is present; otherwise
    INSERT all three right after the `- name: CALDAV_URL` anchor confirmed
    present in the rendered engine env. Idempotent (a re-deploy re-patches to the
    same values). Returns (src, 0) when the anchor is absent — never corrupts."""
    username = JELLYFIN_SOLARIS_USER
    url_base = (
        os.environ.get("DEADLINES_SYNC_URL_BASE") or DEADLINES_SYNC_URL_BASE_DEFAULT
    )

    if re.search(r"- name: SYNC_DAV_PASSWORD\n\s+value:", src):
        # Present already — patch USERNAME + PASSWORD in place, and ensure the
        # URL base is present/updated too (it may not be a sibling).
        new, _ = _patch_env_value(src, "SYNC_DAV_USERNAME", username)
        new, n = _patch_env_value(new, "SYNC_DAV_PASSWORD", password)
        if re.search(r"- name: DEADLINES_SYNC_URL_BASE\n\s+value:", new):
            new, _ = _patch_env_value(new, "DEADLINES_SYNC_URL_BASE", url_base)
        else:
            new = _insert_after_caldav_anchor(
                new, [("DEADLINES_SYNC_URL_BASE", url_base)]
            )
        return new, n

    # Absent — insert all three after the CALDAV_URL anchor.
    new = _insert_after_caldav_anchor(
        src,
        [
            ("SYNC_DAV_USERNAME", username),
            ("SYNC_DAV_PASSWORD", password),
            ("DEADLINES_SYNC_URL_BASE", url_base),
        ],
    )
    return new, (1 if new != src else 0)


def apply_sync_dav_credential_to_engine(password: str) -> bool:
    """Wire the DAV write account into the deployed solaris.yml — the SAME
    `solaris` LLDAP identity + managed password the Jellyfin converge already
    persists/resets (#997 / #1010). owner_only scopes it to `/solaris/*`, so no
    new user, no new password, no shared-auth change. Best-effort; returns True
    when the manifest now carries the password."""
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "sync-dav", "solaris.yml not found at expected path", path=pod_yml)
        return False
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "sync-dav", "could not read solaris.yml", error=str(e))
        return False
    new, n = _patched_sync_dav_yaml(src, password)
    if n == 0:
        jlog(
            "warn",
            "sync-dav",
            "could not wire SYNC_DAV env into solaris.yml — neither a "
            "SYNC_DAV_PASSWORD entry to patch nor a CALDAV_URL anchor to insert "
            "after was found; the DAV sync stays dormant",
            path=pod_yml,
        )
        return False
    if new == src:
        return True
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "sync-dav", "could not write patched solaris.yml", error=str(e))
        return False
    jlog("info", "sync-dav", "stamped managed solaris DAV credential into solaris.yml")
    return True


def converge_jellyfin_credential(data_dir: str) -> str | None:
    """Self-heal the Jellyfin `solaris` service-user credential (#626): persist a
    managed password, reset the lldap user to it, and stamp it into the deployed
    pod env so a template render no longer zeroes auth. Idempotent + best-effort.
    Returns the password (so the caller can wire the HA integration with the same
    value), or None when no password could be persisted."""
    password = _persisted_jellyfin_password(data_dir)
    if not password:
        return None
    reset_lldap_solaris_password(password)
    apply_jellyfin_password_to_engine(password)
    # The DAV write account (#997 / #1010) is the SAME solaris identity: reuse
    # its managed password so the engine authenticates its `/solaris/*` PUTs.
    apply_sync_dav_credential_to_engine(password)
    return password


HOUSEHOLD_CALENDAR_UID_FILE = ".household-calendar-uid"


def _persisted_household_calendar_uid(data_dir: str) -> str:
    """The operator's household-calendar resident uid, persisted at
    <data_dir>/solarisbay/.household-calendar-uid.

    ServiceBay's renderer prunes the `HOUSEHOLD_CALENDAR_UID` template.yml env
    entry (like the SYNC_DAV block, #1011) and drops the install-variable override
    for a newly-added var — so the pod env can't carry it. We instead persist the
    operator's choice to a file (seeded from the env when SB DOES pass it, reused
    after) and stamp it into the pod ourselves. Returns "" when neither a file nor
    the env provides one — household items then keep the principal-less `household`
    uid (documented no-op)."""
    path = os.path.join(data_dir, "solarisbay", HOUSEHOLD_CALENDAR_UID_FILE)
    from_env = os.environ.get("HOUSEHOLD_CALENDAR_UID", "").strip()
    if from_env:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(from_env + "\n")
            os.chmod(path, 0o600)
        except OSError as e:
            jlog(
                "warn", "household-cal", "could not persist household uid", error=str(e)
            )
        return from_env
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def apply_household_calendar_uid_to_engine(data_dir: str) -> bool:
    """Stamp HOUSEHOLD_CALENDAR_UID into the deployed solaris.yml so the nightly
    deadlines sync routes household-wide items to the primary resident's calendar
    (#1011) — the renderer prunes the template.yml entry, so we insert/patch it
    ourselves (same shape as the SYNC_DAV wiring). No-op when unconfigured;
    best-effort. Returns True when the manifest now carries the value."""
    uid = _persisted_household_calendar_uid(data_dir)
    if not uid:
        return False
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "household-cal", "solaris.yml not found", path=pod_yml)
        return False
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "household-cal", "could not read solaris.yml", error=str(e))
        return False
    if re.search(r"- name: HOUSEHOLD_CALENDAR_UID\n\s+value:", src):
        new, n = _patch_env_value(src, "HOUSEHOLD_CALENDAR_UID", uid)
    else:
        new = _insert_after_caldav_anchor(src, [("HOUSEHOLD_CALENDAR_UID", uid)])
        n = 1 if new != src else 0
    if n == 0:
        jlog(
            "warn",
            "household-cal",
            "could not wire HOUSEHOLD_CALENDAR_UID into solaris.yml — no entry to "
            "patch nor a CALDAV_URL anchor to insert after",
            path=pod_yml,
        )
        return False
    if new == src:
        return True
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "household-cal", "could not write solaris.yml", error=str(e))
        return False
    jlog(
        "info",
        "household-cal",
        "stamped HOUSEHOLD_CALENDAR_UID into solaris.yml",
        uid=uid,
    )
    return True


def _patch_or_insert_env(src: str, name: str, value: str) -> str:
    """PATCH `name`'s value in place, or INSERT it after the CALDAV_URL anchor
    when absent (the renderer prunes env entries whose var resolves empty)."""
    if re.search(rf"- name: {re.escape(name)}\n\s+value:", src):
        new, _ = _patch_env_value(src, name, value)
        return new
    return _insert_after_caldav_anchor(src, [(name, value)])


def _patched_caldav_read_yaml(src: str, uid: str, password: str) -> tuple[str, int]:
    """Point the CalDAV READ ingest at the resident's OWN `/<uid>/solaris/`
    collection — the one collection `solaris` may legally read under the
    from_file rights (#524, option a). Reuses CALDAV_USERNAME=solaris + the
    managed DAV password; CARDDAV stays off (solaris has no legal contacts
    collection). Returns (new_text, n) with n>0 when CALDAV_URL now points at
    the collection. Returns (src, 0) when the CALDAV_URL anchor is absent."""
    base = (
        os.environ.get("DEADLINES_SYNC_URL_BASE") or DEADLINES_SYNC_URL_BASE_DEFAULT
    ).rstrip("/")
    url = f"{base}/{uid}/solaris/"
    new, n = _patch_env_value(src, "CALDAV_URL", url)
    if n == 0:
        return src, 0
    new = _patch_or_insert_env(new, "CALDAV_USERNAME", JELLYFIN_SOLARIS_USER)
    new = _patch_or_insert_env(new, "CALDAV_PASSWORD", password)
    return new, n


def apply_caldav_read_to_engine(data_dir: str, password: str) -> bool:
    """Wire the CalDAV read ingest to the resident's own `/<uid>/solaris/` mirror
    (#524, option a) so Solaris re-ingests its own written deadlines/tasks into
    `events`. Reuses the managed `solaris` DAV creds; the renderer prunes these
    template.yml entries, so we stamp them ourselves. No-op when no household uid
    is configured or no password is available; best-effort."""
    if not password:
        return False
    uid = (
        _persisted_household_calendar_uid(data_dir)
        or os.environ.get("DEFAULT_UID", "").strip()
    )
    if not uid:
        return False
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "caldav-read", "solaris.yml not found", path=pod_yml)
        return False
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "caldav-read", "could not read solaris.yml", error=str(e))
        return False
    new, n = _patched_caldav_read_yaml(src, uid, password)
    if n == 0:
        jlog(
            "warn",
            "caldav-read",
            "CALDAV_URL anchor not found in solaris.yml — read ingest stays off",
            path=pod_yml,
        )
        return False
    if new == src:
        return True
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "caldav-read", "could not write solaris.yml", error=str(e))
        return False
    jlog(
        "info",
        "caldav-read",
        "stamped CalDAV read ingest at the resident's /<uid>/solaris/ mirror",
        uid=uid,
    )
    return True


# ── Paperless managed admin + DRF token converge (#1034) ─────────────────────
# The #931 PaperlessIngest handoff no-op'd on the box: paperless is SSO-only
# (Authelia remote-user, no native login), so there was no API-token-bearing
# user and PAPERLESS_URL/TOKEN were empty in the engine. Option A (maintainer
# 2026-07-23): provision a MANAGED paperless superuser (PAPERLESS_ADMIN_USER/
# PASSWORD, template.yml) whose password we own host-side, then MINT that
# admin's DRF API token via /api/token/ and STAMP PAPERLESS_URL + PAPERLESS_TOKEN
# into the engine pod (the renderer prunes empty env, so we insert/patch like the
# SYNC_DAV wiring). Persist the token too so it survives a render that prunes it.
# Idempotent + best-effort + a clean no-op when paperless isn't installed.
PAPERLESS_ADMIN_PASSWORD_FILE = ".paperless-admin-password"
PAPERLESS_TOKEN_FILE = ".paperless-token"
PAPERLESS_URL_DEFAULT = "http://127.0.0.1:8000"
PAPERLESS_ADMIN_USER_DEFAULT = "solaris"
PAPERLESS_CONTAINER = os.environ.get("PAPERLESS_CONTAINER", "paperless-webserver")


def _persisted_paperless_password(data_dir: str) -> str | None:
    """Read/mint the managed paperless admin password, persisted at
    <data_dir>/solarisbay/.paperless-admin-password (0600).

    Generates a strong URL-safe value once if absent, reuses it afterwards (so
    every later deploy reconciles the SAME password). Returns None only when the
    dir can't be written. Never logs the value. Mirrors
    _persisted_jellyfin_password."""
    path = os.path.join(data_dir, "solarisbay", PAPERLESS_ADMIN_PASSWORD_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    password = _secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(password + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        jlog(
            "warn",
            "paperless",
            "could not persist paperless admin password",
            error=str(e),
        )
        return None
    jlog("info", "paperless", "minted managed paperless admin password")
    return password


def _persist_paperless_token(data_dir: str, token: str) -> None:
    """Persist the minted paperless DRF token at
    <data_dir>/solarisbay/.paperless-token (0600) so it survives a render that
    prunes PAPERLESS_TOKEN from the pod env. Best-effort; never logs the value."""
    path = os.path.join(data_dir, "solarisbay", PAPERLESS_TOKEN_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        jlog("warn", "paperless", "could not persist paperless token", error=str(e))


def _read_persisted_paperless_token(data_dir: str) -> str:
    path = os.path.join(data_dir, "solarisbay", PAPERLESS_TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def mint_paperless_token(
    paperless_url: str, username: str, password: str
) -> str | None:
    """Mint the paperless admin's DRF API token by POSTing the managed admin
    credentials to paperless `/api/token/` (DRF obtain_auth_token), which returns
    {"token": "…"}. Returns the token, or None when paperless isn't reachable or
    the credentials aren't (yet) accepted. Best-effort — never raises."""
    status, body = post_json(
        f"{paperless_url.rstrip('/')}/api/token/",
        {"username": username, "password": password},
        timeout=15,
    )
    token = body.get("token") if isinstance(body, dict) else None
    if status == 200 and isinstance(token, str) and token:
        return token
    jlog(
        "info",
        "paperless",
        "paperless token mint did not return a token",
        status=status,
    )
    return None


def create_paperless_superuser(username: str, password: str) -> bool:
    """Create the managed paperless superuser via `manage.py createsuperuser
    --noinput` inside the webserver container, with DJANGO_SUPERUSER_PASSWORD in
    the exec env. paperless-ngx only bootstraps PAPERLESS_ADMIN_USER/PASSWORD on
    first DB init, so an existing DB (or a pruned pod env) never gets the admin —
    this creates it so the DRF token mint can succeed. Idempotent: a non-zero exit
    means the user already exists, which is fine. Best-effort — never raises."""
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                "-e",
                f"DJANGO_SUPERUSER_PASSWORD={password}",
                PAPERLESS_CONTAINER,
                "python3",
                "manage.py",
                "createsuperuser",
                "--noinput",
                "--username",
                username,
                "--email",
                f"{username}@localhost",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog("info", "paperless", "createsuperuser did not run", error=str(e))
        return False
    if proc.returncode == 0:
        jlog("info", "paperless", "created managed paperless superuser", user=username)
        return True
    # Non-zero exit is expected when the user already exists — not fatal.
    jlog(
        "info",
        "paperless",
        "createsuperuser non-zero exit (user likely already exists)",
        user=username,
        stderr=proc.stderr[:200],
    )
    return False


def _patch_or_insert_paperless_env(src: str, url: str, token: str) -> tuple[str, int]:
    """Wire PAPERLESS_URL + PAPERLESS_TOKEN into the engine container's env list
    (pure). PATCH in place when present; else INSERT after the CALDAV_URL anchor
    (the renderer prunes empty env). Returns (new_text, n) with n>0 when the
    manifest now carries the token. Returns (src, 0) when the anchor is absent."""
    new = _patch_or_insert_env(src, "PAPERLESS_URL", url)
    if re.search(r"- name: PAPERLESS_TOKEN\n\s+value:", new):
        new, n = _patch_env_value(new, "PAPERLESS_TOKEN", token)
    else:
        after = _insert_after_caldav_anchor(new, [("PAPERLESS_TOKEN", token)])
        n = 1 if after != new else 0
        new = after
    return new, n


def apply_paperless_credential_to_engine(data_dir: str, url: str, token: str) -> bool:
    """Stamp PAPERLESS_URL + PAPERLESS_TOKEN into the deployed solaris.yml so the
    #931 ingest handoff runs for real (#1034) — the renderer prunes empty env, so
    we insert/patch like the SYNC_DAV wiring. Best-effort; returns True when the
    manifest now carries the token."""
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog(
            "warn", "paperless", "solaris.yml not found at expected path", path=pod_yml
        )
        return False
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "paperless", "could not read solaris.yml", error=str(e))
        return False
    new, n = _patch_or_insert_paperless_env(src, url, token)
    if n == 0:
        jlog(
            "warn",
            "paperless",
            "could not wire PAPERLESS env into solaris.yml — no PAPERLESS_TOKEN "
            "entry to patch nor a CALDAV_URL anchor to insert after",
            path=pod_yml,
        )
        return False
    if new == src:
        return True
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "paperless", "could not write patched solaris.yml", error=str(e))
        return False
    jlog(
        "info", "paperless", "stamped PAPERLESS_URL + PAPERLESS_TOKEN into solaris.yml"
    )
    return True


def converge_paperless_credential(data_dir: str) -> bool:
    """Make the #931 paperless-ingest handoff run for real (#1034, option A):
    persist a managed paperless admin password, mint that admin's DRF API token
    once paperless is reachable, and stamp PAPERLESS_URL + PAPERLESS_TOKEN into
    the deployed solaris.yml (persisting the token so a later render can't prune
    it). Idempotent + best-effort + a clean no-op when paperless isn't installed
    or reachable. Returns True when the engine now carries a paperless token."""
    password = _persisted_paperless_password(data_dir)
    if not password:
        return False
    paperless_url = env("PAPERLESS_URL", PAPERLESS_URL_DEFAULT)
    admin_user = env("PAPERLESS_ADMIN_USER", PAPERLESS_ADMIN_USER_DEFAULT)
    token = mint_paperless_token(paperless_url, admin_user, password)
    if not token:
        # Mint failed — most often because the managed superuser doesn't exist
        # yet (paperless only bootstraps PAPERLESS_ADMIN_USER/PASSWORD on first
        # DB init, and that env is pruned on a re-install; #1036). Create it and
        # retry the mint once before falling back.
        if create_paperless_superuser(admin_user, password):
            token = mint_paperless_token(paperless_url, admin_user, password)
    if not token:
        # paperless not installed / not up yet / creds not accepted — fall back
        # to a previously persisted token so a transient outage doesn't drop the
        # engine's paperless wiring; otherwise no-op cleanly.
        token = _read_persisted_paperless_token(data_dir)
        if not token:
            jlog("info", "paperless", "no paperless token — ingest wiring stays off")
            return False
    else:
        _persist_paperless_token(data_dir, token)
    return apply_paperless_credential_to_engine(data_dir, paperless_url, token)


def _radicale_rights_heredoc(ind: str) -> str:
    """The `cat > /config/rights <<'EOF' … EOF` shell heredoc, every line at the
    block scalar's `ind` (so YAML strips it to column 0 and the `EOF` delimiter
    terminates)."""
    return (
        f"{ind}cat > {RADICALE_RIGHTS_FILE} <<'EOF'\n"
        + "".join(
            (f"{ind}{line}\n" if line else "\n") for line in _RADICALE_RIGHTS_BODY
        )
        + f"{ind}EOF\n"
    )


def _patched_radicale_rights_yaml(src: str) -> tuple[str, int]:
    """Converge the radicale pod manifest from `owner_only` to the `from_file`
    ruleset that also lets `solaris` write `<resident>/solaris` (pure).

    Two edits inside the `write-config` initContainer's `/config/config`
    heredoc: flip `[rights] type = owner_only` → `type = from_file` + `file =`,
    and place a second `cat > /config/rights` heredoc carrying _RADICALE_RIGHTS_BODY.
    Both reuse the config heredoc's own indentation (the `| ` block scalar strips
    it, so the emitted shell heredoc's `EOF` lands at column 0).

    CONTENT-aware, not just presence-aware: an existing rights heredoc whose body
    DRIFTS from the desired one is rewritten, so a fix to the ruleset self-heals
    on the next deploy. Idempotent: a manifest already carrying `from_file` + the
    exact desired heredoc returns (src, 0). Returns (src, 0) when the config
    heredoc anchor is absent or the result isn't valid YAML — a bad edit would
    crash-loop radicale."""
    new, _ = re.subn(
        r"(\[rights\]\n([ \t]*))type = owner_only",
        lambda m: (
            f"{m.group(1)}type = from_file\n{m.group(2)}file = " + RADICALE_RIGHTS_FILE
        ),
        src,
    )
    anchor = re.search(
        r"(?P<ind>[ \t]*)cat > /config/config <<'EOF'\n(?:.*\n)*?(?P=ind)EOF\n",
        new,
    )
    if anchor:
        ind = anchor.group("ind")
        desired = _radicale_rights_heredoc(ind)
        existing = re.search(
            re.escape(ind)
            + r"cat > /config/rights <<'EOF'\n(?:.*\n)*?"
            + re.escape(ind)
            + r"EOF\n",
            new,
        )
        if existing:
            if existing.group(0) != desired:  # drifted body → rewrite in place
                new = new[: existing.start()] + desired + new[existing.end() :]
        else:  # absent → insert right after the config heredoc
            new = new[: anchor.end()] + desired + new[anchor.end() :]
    if new == src:
        return src, 0
    # PyYAML is present in the pod image (where this runs) but not in templates
    # CI; skip the validity check there rather than fail import.
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            yaml.safe_load(new)
        except yaml.YAMLError as e:
            jlog(
                "warn",
                "radicale-rights",
                "rights patch produced invalid YAML — abandoning it to avoid a "
                "radicale crash-loop",
                error=str(e),
            )
            return src, 0
    return new, 1


def converge_radicale_rights() -> bool:
    """Grant the `solaris` DAV account write access to `<resident>/solaris` by
    converging Radicale's rights from `owner_only` to an equivalent `from_file`
    ruleset (option A, #997/#1011). Patches the radicale pod manifest's config
    heredoc and restarts radicale ONLY on drift. Idempotent + best-effort:
    skips cleanly when radicale isn't installed, when already converged, or when
    the anchors are absent. Returns True when radicale was (re)configured."""
    pod_yml = os.path.expanduser(RADICALE_POD_YML)
    if not os.path.exists(pod_yml):
        jlog("info", "radicale-rights", "radicale.yml absent — radicale not installed")
        return False
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "radicale-rights", "could not read radicale.yml", error=str(e))
        return False
    new, n = _patched_radicale_rights_yaml(src)
    if n == 0:
        jlog("info", "radicale-rights", "radicale rights already converged — no-op")
        return False
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog("warn", "radicale-rights", "could not write radicale.yml", error=str(e))
        return False
    jlog(
        "info",
        "radicale-rights",
        "granted solaris write to <resident>/solaris (from_file); restarting radicale",
    )
    subprocess.run(
        ["systemctl", "--user", "restart", "radicale.service"],
        check=False,
        capture_output=True,
    )
    return True


# ── durable rights self-heal (#1023) ─────────────────────────────────────────
# converge_radicale_rights() runs only on a solaris deploy, but radicale is a
# ServiceBay-registry service: a radicale RE-RENDER / autoupdate regenerates
# radicale.yml back to stock `owner_only` and wipes /config/rights, so `solaris`
# 403s on PUT /<uid>/solaris/ and the calendar feature breaks until the next
# `install_template solaris`. The engine (host-networked but IN a container)
# can DETECT the revert via the DAV API, but re-patching radicale.yml on the
# HOST and restarting the radicale systemd unit is a host operation the engine
# can't do. So the self-heal is a host-side systemd `.timer` the post-deploy
# installs, which periodically re-runs THIS SAME script in converge-only mode
# (SOLARIS_CONVERGE_ONLY=1 → main() calls just converge_radicale_rights() and
# exits). converge_radicale_rights() is already idempotent + drift-aware, so a
# stock owner_only is re-converged and radicale restarted; an already-converged
# manifest is a no-op. No second copy of the patch logic → no drift.
RADICALE_RIGHTS_SERVICE = "solaris-radicale-rights"
RADICALE_RIGHTS_CONVERGE_SCRIPT = "radicale-rights-converge.py"
RADICALE_RIGHTS_TIMER_INTERVAL = "15min"


def render_radicale_rights_service(script_path: str) -> str:
    """Render the oneshot `.service` that re-runs the post-deploy in converge-
    only mode (pure). SOLARIS_CONVERGE_ONLY=1 short-circuits main() to just the
    radicale-rights converge."""
    return (
        "[Unit]\n"
        "Description=Solaris Radicale rights self-heal (#1023)\n"
        "After=radicale.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Environment=SOLARIS_CONVERGE_ONLY=1\n"
        f"ExecStart=/usr/bin/env python3 {script_path}\n"
    )


def render_radicale_rights_timer() -> str:
    """Render the `.timer` that fires the rights-converge service on a fixed
    cadence and shortly after boot (pure). Persistent= re-fires a missed run
    after the box was off."""
    return (
        "[Unit]\n"
        "Description=Solaris Radicale rights self-heal timer (#1023)\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        f"OnUnitActiveSec={RADICALE_RIGHTS_TIMER_INTERVAL}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def install_radicale_rights_timer(data_dir: str) -> bool:
    """Install the host-side self-heal timer (#1023): copy THIS script to a
    durable data-dir path and write a `.service` + `.timer` that re-run it in
    converge-only mode. Idempotent (rewrites only on drift) + best-effort — a
    failure never blocks the deploy. Returns True when the timer is active."""
    systemd_dir = os.path.expanduser("~/.config/systemd/user")
    script_dst = os.path.join(data_dir, "solarisbay", RADICALE_RIGHTS_CONVERGE_SCRIPT)
    try:
        with open(os.path.realpath(__file__), encoding="utf-8") as f:
            self_src = f.read()
    except OSError as e:
        jlog("warn", "radicale-rights", "could not read self for timer", error=str(e))
        return False
    try:
        os.makedirs(os.path.dirname(script_dst), exist_ok=True)
        with open(script_dst, "w", encoding="utf-8") as f:
            f.write(self_src)
        os.chmod(script_dst, 0o755)
        os.makedirs(systemd_dir, exist_ok=True)
        with open(
            os.path.join(systemd_dir, f"{RADICALE_RIGHTS_SERVICE}.service"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(render_radicale_rights_service(script_dst))
        with open(
            os.path.join(systemd_dir, f"{RADICALE_RIGHTS_SERVICE}.timer"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(render_radicale_rights_timer())
    except OSError as e:
        jlog(
            "warn", "radicale-rights", "could not install self-heal timer", error=str(e)
        )
        return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    started = subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{RADICALE_RIGHTS_SERVICE}.timer"],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "warn",
            "radicale-rights",
            "could not enable self-heal timer",
            error=started.stderr[:300],
        )
        return False
    jlog("info", "radicale-rights", "installed rights self-heal timer", path=script_dst)
    return True


# ── voice pipeline ───────────────────────────────────────────────────────────


def wait_for_chat(chat_port: str, timeout_secs: int = 120) -> bool:
    """Wait for the chat server's /health — the ollama config flow validates
    against the engine facade, so the engine must be up first."""
    deadline = time.time() + timeout_secs
    url = f"http://127.0.0.1:{chat_port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(3)
    jlog("warn", "voice", "chat /health not up within deadline", port=chat_port)
    return False


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout_secs: int = 60) -> bool:
    """Poll a TCP port until it accepts a connection or the deadline passes.

    The gatekeeper's Wyoming STT listener (:10700) boots a few seconds after
    the post-deploy starts wiring (box-observed ~7s, #395), so its entity must
    not be registered before the port answers — otherwise the STT converge
    silently no-ops and speaker-ID never enters the live path."""
    deadline = time.time() + timeout_secs
    while True:
        if _port_open(host, port):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(3)


def _flow_create(
    token: str, handler: str, steps: list[dict[str, object]]
) -> tuple[str, dict | None]:
    """Drive one HA config flow: start it, submit each step's data in order.

    Returns ("created", entry) | ("already", None) | ("failed", last_result).
    """
    status, flow = _ha_post(
        "/api/config/config_entries/flow", token, {"handler": handler}
    )
    if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
        return "failed", flow if isinstance(flow, dict) else None
    result: dict | None = flow
    for step in steps:
        flow_id = result.get("flow_id") if isinstance(result, dict) else None
        if not flow_id:
            break
        status, result = _ha_post(
            f"/api/config/config_entries/flow/{flow_id}", token, step
        )
        if not isinstance(result, dict):
            return "failed", None
        if result.get("type") == "abort":
            reason = str(result.get("reason") or "")
            if "already" in reason:
                return "already", None
            return "failed", result
    if isinstance(result, dict) and result.get("type") == "create_entry":
        return "created", result.get("result") if isinstance(
            result.get("result"), dict
        ) else result
    if isinstance(result, dict) and (fid := result.get("flow_id")):
        _ha_request_delete(f"/api/config/config_entries/flow/{fid}", token)
    return "failed", result


def ensure_wyoming_entry(token: str, label: str, host: str, port: int) -> None:
    """Register a wyoming service (whisper STT / piper TTS) in HA. The wyoming
    flow does NOT de-dup on host+port (box-observed: a second run created a
    duplicate entry), so idempotency is a title check against the existing
    entries — the entry title is the announced wyoming service name."""
    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status == 200 and isinstance(entries, list):
        for e in entries:
            if (
                isinstance(e, dict)
                and e.get("domain") == "wyoming"
                and label in str(e.get("title") or "").lower()
            ):
                jlog("info", "voice", f"wyoming {label}: already", port=port)
                return
    state, result = _flow_create(token, "wyoming", [{"host": host, "port": port}])
    jlog(
        "info" if state in ("created", "already") else "warn",
        "voice",
        f"wyoming {label}: {state}",
        host=host,
        port=port,
        detail=(result or {}).get("reason") if state == "failed" else None,
    )


def _ollama_entry_id(token: str, facade_url: str) -> str:
    """The entry_id of the ollama config entry pointing at the engine facade,
    or ''. Falls back to any ollama entry whose title carries the facade URL."""
    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status != 200 or not isinstance(entries, list):
        return ""
    candidates = [
        e for e in entries if isinstance(e, dict) and e.get("domain") == "ollama"
    ]
    for e in candidates:
        if facade_url in str(e.get("title") or ""):
            return str(e.get("entry_id") or "")
    if len(candidates) == 1:
        return str(candidates[0].get("entry_id") or "")
    return ""


def _ollama_entry_matches_facade(entry: dict, facade_url: str) -> bool:
    """True when `entry` is the ollama config entry pointing at the engine's
    /ollama facade — match on domain + the facade URL in data.url so an
    unrelated ollama entry (if any) is never clobbered."""
    if entry.get("domain") != "ollama":
        return False
    data = entry.get("data")
    return isinstance(data, dict) and str(data.get("url") or "") == facade_url


def _reassert_ollama_key_in_storage(
    entries: list[object], facade_url: str, api_key: str
) -> tuple[list[object], bool]:
    """Re-assert the facade ollama entry's `data.api_key` = `api_key` (pure).

    Returns (entries, changed). `changed` is True only when a matching entry
    carried a different key — so the caller writes `.storage` only on real
    drift (idempotent). A missing entry is a clean no-op (voice not set up)."""
    changed = False
    for entry in entries:
        if not isinstance(entry, dict) or not _ollama_entry_matches_facade(
            entry, facade_url
        ):
            continue
        if str(entry["data"].get("api_key") or "") != api_key:
            entry["data"]["api_key"] = api_key
            changed = True
    return entries, changed


def reassert_ollama_api_key(token: str, chat_port: str, api_key: str, data_dir: str):
    """Heal a drifted HA ollama-integration api_key so `conversation.sol` can't
    red-blink (#557).

    The ollama integration (provides `conversation.sol`) stores an `api_key`;
    when it drifts from the pod's current SOLARIS_API_KEY the integration goes
    `setup_error: unauthorized (401)` → `conversation.sol` unavailable → the
    Assist pipeline red-blinks. The integration has NO async_step_reconfigure,
    so it can't self-heal — we re-assert the key directly in HA's
    `.storage/core.config_entries` (atomic tmp+rename), matching the facade
    ollama entry on domain + data.url. A running HA holds config entries in
    memory and won't re-read `.storage` on its own (and would overwrite our
    edit on its next write), so on a real change we ask HA to restart — HA then
    reloads the entry from the patched file and the 401'd setup re-attempts.
    (main()'s final restart is the `solaris` service, not HA, so it can't do
    this — this is the box-confirmed manual fix: patch `.storage` + restart HA.)

    Idempotent (writes only on real drift, so a converged box never restarts HA)
    + fail-soft (a missing entry / file is a clean no-op — voice may not be set
    up on this box). The api_key is never logged."""
    if not api_key:
        return
    facade_url = f"http://127.0.0.1:{chat_port}/ollama"
    storage = os.path.join(
        data_dir,
        "home-assistant",
        "homeassistant",
        ".storage",
        "core.config_entries",
    )
    try:
        with open(storage, encoding="utf-8") as f:
            store = json.load(f)
    except OSError:
        jlog(
            "info",
            "voice",
            "no HA .storage config_entries — ollama key re-assert skipped",
        )
        return
    except json.JSONDecodeError as e:
        jlog("warn", "voice", "HA config_entries not valid JSON", error=str(e))
        return
    entries = store.get("data", {}).get("entries")
    if not isinstance(entries, list):
        jlog("warn", "voice", "HA config_entries has no entries list")
        return
    _, changed = _reassert_ollama_key_in_storage(entries, facade_url, api_key)
    if not changed:
        jlog("info", "voice", "ollama api_key already current — no re-assert")
        return
    tmp = f"{storage}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f)
        os.replace(tmp, storage)
    except OSError as e:
        jlog("warn", "voice", "could not write patched HA config_entries", error=str(e))
        return
    jlog("info", "voice", "re-asserted ollama api_key in HA .storage (heals 401 drift)")
    # HA won't re-read the patched .storage while running (and would clobber it
    # on its next write) — restart so it reloads the entry with the fixed key.
    status, _ = _ha_post("/api/services/homeassistant/restart", token, {})
    jlog(
        "info" if status == 200 else "warn",
        "voice",
        "requested HA restart to reload ollama entry",
        status=status,
    )


def ensure_conversation_agent(token: str, chat_port: str, api_key: str) -> str:
    """Wire Solaris as an HA conversation agent: an `ollama` config entry pointing
    at the engine's /ollama facade + a `conversation` subentry on model `solaris`.

    HA 2026.6's openai_conversation has no custom base_url; its ollama
    integration takes a free URL + Bearer api_key and speaks exactly the
    facade's protocol (box-verified 2026-06-12). Returns the conversation
    entity_id, or ''.
    """
    facade_url = f"http://127.0.0.1:{chat_port}/ollama"
    state, result = _flow_create(
        token, "ollama", [{"url": facade_url, "api_key": api_key}]
    )
    if state == "failed":
        jlog("warn", "voice", "ollama entry flow failed", detail=result)
        return ""
    entry_id = ""
    if state == "created" and isinstance(result, dict):
        entry_id = str(result.get("entry_id") or "")
    if not entry_id:
        entry_id = _ollama_entry_id(token, facade_url)
    if not entry_id:
        jlog("warn", "voice", "no ollama entry id resolvable")
        return ""

    # Conversation entity already there? (idempotent re-deploy)
    existing = _find_entity(token, "conversation.", CONVERSATION_AGENT_NAME.lower())
    if existing:
        return existing

    # A freshly-created entry loads asynchronously and the subentry flow
    # aborts `entry_not_loaded` until it has — retry briefly.
    result: dict | None = None
    for attempt in range(5):
        if attempt:
            time.sleep(3)
        status, flow = _ha_post(
            "/api/config/config_entries/subentries/flow",
            token,
            {"handler": [entry_id, "conversation"]},
        )
        if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
            jlog(
                "warn", "voice", "conversation subentry flow not started", status=status
            )
            return ""
        status, result = _ha_post(
            f"/api/config/config_entries/subentries/flow/{flow['flow_id']}",
            token,
            {
                "name": CONVERSATION_AGENT_NAME,
                "model": ENGINE_MODEL,
                "prompt": VOICE_PROMPT,
            },
        )
        if (
            status == 200
            and isinstance(result, dict)
            and result.get("type") == "create_entry"
        ):
            break
        if isinstance(result, dict) and result.get("reason") == "entry_not_loaded":
            continue
        break
    if not isinstance(result, dict) or result.get("type") != "create_entry":
        jlog("warn", "voice", "conversation subentry not created", detail=result)
        return ""
    jlog("info", "voice", "created Solaris conversation agent", entry_id=entry_id)
    # The conversation entity registers asynchronously — poll briefly.
    for _ in range(10):
        entity = _find_entity(token, "conversation.", CONVERSATION_AGENT_NAME.lower())
        if entity:
            return entity
        time.sleep(2)
    return ""


def _find_entity(token: str, prefix: str, needle: str = "") -> str:
    """First entity_id with `prefix` (and `needle` in the id or friendly
    name), or ''."""
    status, states = _ha_get("/api/states", token)
    if status != 200 or not isinstance(states, list):
        return ""
    for s in states:
        if not isinstance(s, dict):
            continue
        entity_id = str(s.get("entity_id") or "")
        if not entity_id.startswith(prefix):
            continue
        friendly = str((s.get("attributes") or {}).get("friendly_name") or "")
        if not needle or needle in entity_id.lower() or needle in friendly.lower():
            return entity_id
    return ""


class HAWebSocket:
    """Minimal RFC6455 client for HA's /api/websocket (stdlib only).

    Only what the pipeline storage API needs: auth, send command, await its
    result. The assist_pipeline collection has no REST surface — websocket is
    the only way to create a pipeline."""

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 8123):
        self._token = token
        self._sock = socket.create_connection((host, port), timeout=15)
        self._sock.settimeout(15)
        self._buf = b""
        self._next_id = 1
        self._handshake(host)
        self._auth()

    def _handshake(self, host: str) -> None:
        key = base64.b64encode(_secrets.token_bytes(16)).decode()
        self._sock.sendall(
            (
                f"GET /api/websocket HTTP/1.1\r\nHost: {host}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake EOF")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket upgrade refused")
        self._buf = response.split(b"\r\n\r\n", 1)[1]

    def _auth(self) -> None:
        msg = self._recv_json()
        if msg.get("type") != "auth_required":
            raise ConnectionError(f"unexpected hello: {msg}")
        self._send_json({"type": "auth", "access_token": self._token})
        msg = self._recv_json()
        if msg.get("type") != "auth_ok":
            raise ConnectionError(f"auth failed: {msg}")

    def cmd(self, payload: dict[str, object]) -> dict:
        """Send one command; return its result message (raises on error)."""
        msg_id = self._next_id
        self._next_id += 1
        self._send_json({"id": msg_id, **payload})
        while True:
            msg = self._recv_json()
            if msg.get("id") == msg_id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise RuntimeError(f"HA command failed: {msg.get('error')}")
                return msg.get("result") or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    # -- frames ---------------------------------------------------------------

    def _send_json(self, obj: dict[str, object]) -> None:
        payload = json.dumps(obj).encode()
        mask = _secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 1 << 16:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket EOF")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_json(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exact(8))
            payload = self._read_exact(length)
            if opcode == 0x9:  # ping -> pong
                self._sock.sendall(struct.pack("!BB", 0x8A, 0x80) + b"\x00" * 4)
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket closed by server")
            message += payload
            if b1 & 0x80:  # FIN
                return json.loads(message.decode("utf-8"))


def install_wake_word_model(data_dir: str, custom_dir: str, model_id: str) -> bool:
    """Copy the trained `<model_id>.tflite` into the voice service's custom-model
    dir so wyoming-openwakeword loads it (#407; servicebay#1832 ships the dir).

    The model is a brand asset produced offline by scripts/train-wake-word.py —
    it is NOT built here. Source path is `<data_dir>/solaris/wakeword/
    <model_id>.tflite` (committed model transported by ServiceBay, or dropped
    there by an operator). Fail-soft: a missing source logs an actionable
    warning and returns False so the pipeline falls back to no wake word
    (push-to-talk) instead of pointing at a model that doesn't exist. Returns
    True only when the model is present in the custom dir afterwards."""
    source = os.path.join(data_dir, "solaris", "wakeword", f"{model_id}.tflite")
    target = os.path.join(custom_dir, f"{model_id}.tflite")
    try:
        with open(source, "rb") as f:
            model = f.read()
    except OSError:
        if os.path.exists(target):
            jlog("info", "wakeword", "custom model already installed", path=target)
            return True
        jlog(
            "warn",
            "wakeword",
            f"no trained '{model_id}' wake-word model — the Assist wake word stays "
            "unset (push-to-talk). Produce it offline with scripts/train-wake-word.py "
            "(openWakeWord training pipeline + Piper synthetic samples), then commit "
            "it to templates/solaris/wakeword/ or drop it on the box, and redeploy.",
            source=source,
        )
        return False
    try:
        with open(target, "rb") as f:
            if f.read() == model:
                return True
    except OSError:
        pass
    try:
        os.makedirs(custom_dir, exist_ok=True)
        with open(target, "wb") as f:
            f.write(model)
    except OSError as e:
        jlog("error", "wakeword", "could not install wake-word model", error=str(e))
        return False
    jlog("info", "wakeword", "installed custom wake-word model", path=target)
    return True


def _wake_word_entity(token: str, model_id: str) -> str:
    """The openWakeWord wyoming `wake_word.*` entity_id, or ''. The wyoming
    integration registers one wake_word entity per openWakeWord connection; the
    individual `<model_id>` is selected via wake_word_id on the pipeline."""
    return (
        _find_entity(token, "wake_word.", model_id)
        or _find_entity(token, "wake_word.", "openwakeword")
        or _find_entity(token, "wake_word.", "")
    )


def ensure_assist_pipeline(
    token: str,
    conversation_entity: str,
    prefer_gatekeeper_stt: bool = False,
    wake_word_id: str = "",
) -> bool:
    """Create the "Solaris" Assist pipeline (stt=whisper, conversation=Solaris,
    tts=piper) and make it the preferred pipeline. Then point the Voice PE's
    pipeline select at it. Idempotent on the name.

    With `prefer_gatekeeper_stt` (speaker-ID on, #350) the STT engine is the
    gatekeeper's Wyoming STT entity instead of bare whisper, so the pipeline's
    audio flows through the gatekeeper's speaker-ID resolver. It transcribes
    via whisper internally, so STT output is identical — only the resident is
    additionally resolved and stashed for the engine facade.

    With `wake_word_id` set (#407) the pipeline's server-side wake word is the
    custom openWakeWord model (single word "Solaris", no prefix); the model
    file must already be installed in the voice service. When the model's
    wake_word entity can't be resolved the wake word is left unset (push-to-talk)
    rather than failing."""
    # Needle-match the wyoming engines: the box already carries other tts
    # entities (e.g. a google cloud one) and the pipeline must ride the
    # local whisper/piper pair. Fresh wyoming entries register their
    # entities asynchronously — poll briefly.
    stt_entity = tts_entity = ""
    for attempt in range(10):
        if attempt:
            time.sleep(3)
        if prefer_gatekeeper_stt:
            # The gatekeeper registers a Wyoming STT entity from its ASR
            # program (name solaris-gatekeeper-asr); needle "gatekeeper".
            # Fall back to whisper if it hasn't materialised yet.
            stt_entity = _find_entity(token, "stt.", "gatekeeper") or _find_entity(
                token, "stt.", "whisper"
            )
        else:
            stt_entity = _find_entity(token, "stt.", "whisper") or _find_entity(
                token, "stt.", "wyoming"
            )
        # Solaris's Martin voice when its bridge entity exists (GPU boxes,
        # servicebay#1815); piper otherwise. The two differ in language/
        # voice fields: the bridge announces plain `de` + voice `kokoro`,
        # piper needs the regional `de_DE` (a bare `de` 500s announces).
        tts_entity = (
            _find_entity(token, "tts.", "openai")
            or _find_entity(token, "tts.", "piper")
            or _find_entity(token, "tts.", "wyoming")
        )
        if stt_entity and tts_entity:
            break
    if not (stt_entity and tts_entity and conversation_entity):
        jlog(
            "warn",
            "voice",
            "pipeline prerequisites missing",
            stt=stt_entity,
            tts=tts_entity,
            conversation=conversation_entity,
        )
        return False
    try:
        ws = HAWebSocket(token)
    except (OSError, ConnectionError, RuntimeError) as e:
        jlog("warn", "voice", "HA websocket unavailable", error=str(e))
        return False
    try:
        listed = ws.cmd({"type": "assist_pipeline/pipeline/list"})
        pipelines = listed.get("pipelines") or []
        existing = next(
            (
                p
                for p in pipelines
                if isinstance(p, dict) and p.get("name") == PIPELINE_NAME
            ),
            None,
        )
        martin = "openai" in tts_entity
        tts_fields = {
            "tts_engine": tts_entity,
            # The Martin bridge announces plain `de` + voice `kokoro`; wyoming
            # piper announces regional voice codes — there a bare "de" makes
            # every announce/TTS call 500 with "Language 'de' not supported"
            # (box-verified 2026-06-12).
            "tts_language": "de" if martin else "de_DE",
            # The bridge announces a Kokoro VOICE name (the model name on the
            # OpenAI side is "kokoro" — wrong here: a kokoro tts_voice makes
            # every pipeline TTS fail silently, box bridge log 2026-06-12). The
            # admin-selected voice (#368) converges here; default Martin.
            "tts_voice": selected_tts_voice() if martin else None,
        }
        # #407: set the custom "Solaris" wake word when its model is installed
        # and the openWakeWord wyoming entity is resolvable; else leave it unset
        # (push-to-talk) rather than pointing at a wake word that doesn't exist.
        wake_entity = _wake_word_entity(token, wake_word_id) if wake_word_id else ""
        if wake_word_id and not wake_entity:
            jlog(
                "warn",
                "voice",
                "no openWakeWord wake_word entity — wake word stays unset",
                wake_word_id=wake_word_id,
            )
        wake_fields = {
            "wake_word_entity": wake_entity or None,
            "wake_word_id": wake_word_id if wake_entity else None,
        }
        if existing is None:
            created = ws.cmd(
                {
                    "type": "assist_pipeline/pipeline/create",
                    "name": PIPELINE_NAME,
                    "language": "de",
                    "conversation_engine": conversation_entity,
                    "conversation_language": "de",
                    "stt_engine": stt_entity,
                    "stt_language": "de",
                    **tts_fields,
                    **wake_fields,
                }
            )
            pipeline_id = created.get("id")
            jlog("info", "voice", "created Solaris assist pipeline", id=pipeline_id)
        else:
            pipeline_id = existing.get("id")
            # Converge an existing pipeline onto the preferred TTS (a GPU box
            # may have been wired with piper before the Martin units landed)
            # and onto the preferred STT (speaker-ID toggled on a redeploy
            # moves it from whisper to the gatekeeper, #350).
            if (
                existing.get("tts_engine") != tts_entity
                or existing.get("tts_voice") != tts_fields["tts_voice"]
                or existing.get("stt_engine") != stt_entity
                or existing.get("wake_word_entity") != wake_fields["wake_word_entity"]
                or existing.get("wake_word_id") != wake_fields["wake_word_id"]
            ):
                upd = {
                    k: existing.get(k)
                    for k in (
                        "conversation_engine",
                        "conversation_language",
                        "language",
                        "name",
                        "stt_engine",
                        "stt_language",
                        "tts_engine",
                        "tts_language",
                        "tts_voice",
                        "wake_word_entity",
                        "wake_word_id",
                    )
                }
                upd.update(tts_fields)
                upd.update(wake_fields)
                upd["stt_engine"] = stt_entity
                ws.cmd(
                    {
                        "type": "assist_pipeline/pipeline/update",
                        "pipeline_id": pipeline_id,
                        **upd,
                    }
                )
                jlog(
                    "info",
                    "voice",
                    "Solaris pipeline converged",
                    tts=tts_entity,
                    stt=stt_entity,
                )
            else:
                jlog("info", "voice", "Solaris assist pipeline already present")
        if pipeline_id:
            ws.cmd(
                {
                    "type": "assist_pipeline/pipeline/set_preferred",
                    "pipeline_id": pipeline_id,
                }
            )
    except (OSError, ConnectionError, RuntimeError) as e:
        jlog("warn", "voice", "pipeline create/set_preferred failed", error=str(e))
        return False
    finally:
        ws.close()

    _assign_pe_pipeline(token)
    return True


def _assign_pe_pipeline(token: str) -> None:
    """Point the Voice PE's pipeline select(s) at the Solaris pipeline (fail-soft
    — a select on `preferred` already follows the preferred pipeline, this
    just pins it explicitly; the box PE exposes two assistant selects)."""
    status, states = _ha_get("/api/states", token)
    if status != 200 or not isinstance(states, list):
        return
    selects = [
        str(s.get("entity_id"))
        for s in states
        if isinstance(s, dict)
        and str(s.get("entity_id") or "").startswith("select.")
        and "voice" in str(s.get("entity_id"))
        and "assist" in str(s.get("entity_id"))
    ]
    if not selects:
        jlog("info", "voice", "no PE pipeline select entity found — skipping assign")
        return
    for select in selects:
        status, _ = _ha_post(
            "/api/services/select/select_option",
            token,
            {"entity_id": select, "option": PIPELINE_NAME},
        )
        jlog(
            "info" if status == 200 else "warn",
            "voice",
            "PE pipeline select",
            entity=select,
            option=PIPELINE_NAME,
            status=status,
        )


def wire_voice_pipeline(
    token: str, chat_port: str, api_key: str, data_dir: str = "/mnt/data"
) -> None:
    """The Phase-2 wiring: wyoming STT/TTS + conversation agent + pipeline."""
    if not token:
        jlog("info", "voice", "no HA token — skipping voice pipeline wiring")
        return
    ensure_wyoming_entry(token, "whisper", "127.0.0.1", 10300)
    ensure_wyoming_entry(token, "piper", "127.0.0.1", 10200)
    # Solaris's Martin voice (servicebay#1815): the wyoming_openai bridge only
    # runs on GPU boxes — register it when it listens, skip silently when not.
    if _port_open("127.0.0.1", 10203):
        ensure_wyoming_entry(token, "openai", "127.0.0.1", 10203)
    # Speaker-ID on the live path (#350, approach b): register the gatekeeper
    # as a Wyoming STT engine so the Assist pipeline's audio flows through it
    # — it transcribes, resolves the speaking resident, and stashes the uid
    # for the engine facade. The gatekeeper listens on its Wyoming port (the
    # same listener satellites use), reachable over host loopback. TTS stays
    # piper/Martin and the conversation agent stays Solaris.
    #
    # The flag + port live on the gatekeeper container: SB does NOT export the
    # template vars to this script (see _container_env), so read them from the
    # container, falling back to a process env / default for local runs.
    speaker_id = _truthy(
        env("SOLARIS_SPEAKER_ID_ENABLED")
        or gatekeeper_container_env("SOLARIS_SPEAKER_ID_ENABLED")
    )
    if speaker_id:
        gk_port = int(
            env("GATEKEEPER_PORT")
            or gatekeeper_container_env("GATEKEEPER_PORT")
            or "10700"
        )
        # The gatekeeper's Wyoming STT listener boots ~7s after this wiring runs
        # (#395); register it only once :gk_port answers, else the STT converge
        # silently no-ops and speaker-ID stays out of the live STT path.
        if _wait_for_port("127.0.0.1", gk_port):
            ensure_wyoming_entry(token, "gatekeeper", "127.0.0.1", gk_port)
        else:
            jlog(
                "warn",
                "voice",
                "gatekeeper Wyoming STT not reachable — skipping STT rewire",
                port=gk_port,
            )
            speaker_id = False
    if not wait_for_chat(chat_port):
        jlog("warn", "voice", "engine facade not up — conversation agent skipped")
        return
    # #407: install the custom "Solaris" wake-word model and wire it as the
    # default. Configurable; model file is produced offline (fail-soft if absent).
    wake_word_id = env("WAKE_WORD_MODEL", WAKE_WORD_MODEL)
    custom_dir = env("OPENWAKEWORD_CUSTOM_DIR", OPENWAKEWORD_CUSTOM_DIR)
    wake_word = (
        wake_word_id
        if wake_word_id and install_wake_word_model(data_dir, custom_dir, wake_word_id)
        else ""
    )
    # The model alone is inert: HA only exposes a `wake_word.*` entity (which
    # ensure_assist_pipeline needs to pin the wake word) once the openWakeWord
    # wyoming service is registered as an integration. Register it after the
    # model is in the custom dir so HA enumerates "solaris" on connect. Gated on
    # the port like the TTS bridge — the openWakeWord container may be absent.
    if wake_word and _port_open("127.0.0.1", 10400):
        ensure_wyoming_entry(token, "openwakeword", "127.0.0.1", 10400)
    conversation_entity = ensure_conversation_agent(token, chat_port, api_key)
    if conversation_entity:
        ensure_assist_pipeline(
            token,
            conversation_entity,
            prefer_gatekeeper_stt=speaker_id,
            wake_word_id=wake_word,
        )
    # Re-assert the ollama entry's stored api_key = the current SOLARIS_API_KEY
    # (#557): the integration has no reconfigure flow, so a drifted key leaves it
    # setup_error/401 → conversation.sol unavailable → the pipeline red-blinks.
    # Last, so the pipeline wiring above finishes before a (drift-only) HA
    # restart; a converged key is a no-op and never restarts HA.
    reassert_ollama_api_key(token, chat_port, api_key, data_dir)


# ════════════════════════════════════════════════════════════════════════════
# 3. ADMIN MCP TOKEN — minted via the SB API, dropped as a file the engine
#    reads lazily.
# ════════════════════════════════════════════════════════════════════════════


def probe_admin_token(token: str, mcp_url: str) -> bool:
    """Live-validate an admin bearer against `/mcp`. 200 = ok; 401 = stale.
    Connection failure returns True (don't churn tokens on a hiccup)."""
    if not token or not mcp_url:
        return False
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "solaris-post-deploy", "version": "1"},
        },
    }
    req = urllib.request.Request(
        mcp_url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        return e.code != 401
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return True


def mint_admin_token(
    sb_api: str, attempts: int = 4, backoff_s: float = 3.0
) -> str | None:
    """Mint a read+lifecycle+mutate ServiceBay-MCP token for the admin
    persona. Retries for the SB readiness race (#126); never persists a
    non-`sb_` fallback."""
    for attempt in range(1, attempts + 1):
        status, body = post_json(
            f"{sb_api}/api/system/api-tokens",
            {"name": ADMIN_TOKEN_NAME, "scopes": ADMIN_MCP_SCOPES},
            timeout=15,
        )
        if status == 200 and isinstance(body, dict):
            secret = body.get("secret")
            if isinstance(secret, str) and SB_MCP_TOKEN_RE.match(secret):
                jlog("info", "admin-mcp", "minted admin SB-MCP token", attempt=attempt)
                return secret
        if attempt < attempts:
            time.sleep(backoff_s)
    jlog("warn", "admin-mcp", "could not mint admin SB-MCP token", attempts=attempts)
    return None


def ensure_read_token_file(data_dir: str) -> bool:
    """Persist the non-expiring read-only SB token at
    <data_dir>/solarisbay/sb-read-token (0600) for the unattended pollers (#818).

    ServiceBay (servicebay#2317) mints the durable read-scoped token itself and
    injects it as the SB_READ_TOKEN env var, revoking+re-minting it each deploy —
    so this just persists that env var to the file; it never self-mints (durable
    creds are the platform's job).

    Idempotent: a present, well-formed file that already matches the injected
    token is kept. Best-effort; a miss just leaves the pollers on the rotating
    admin token (they fall back to SB_MCP_TOKEN_PATH)."""
    path = os.path.join(data_dir, "solarisbay", "sb-read-token")
    existing = ""
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read().strip()
    except OSError:
        pass
    env_token = env("SB_READ_TOKEN").strip()
    # ServiceBay REVOKES + re-mints this token on every deploy (servicebay#2317),
    # so a merely well-formed EXISTING token may already be revoked — keeping it
    # would strand the pollers on a dead credential (they'd 401 against every
    # /napi call). Only keep the file as-is when it already equals the freshly
    # injected token; otherwise OVERWRITE it with the injected one. (The old code
    # kept any well-formed file, which broke #818 from the 2nd deploy onward.)
    if env_token and SB_MCP_TOKEN_RE.match(env_token):
        if existing == env_token:
            jlog(
                "info",
                "read-token",
                "read-only token file already matches injected token",
            )
            return True
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(env_token + "\n")
            os.chmod(path, 0o600)
        except OSError as e:
            jlog("error", "read-token", "could not write read token file", error=str(e))
            return False
        jlog("info", "read-token", "wrote SB_READ_TOKEN to read-only token file")
        return True
    # No token injected (legacy ServiceBay without #2317): keep an existing
    # well-formed file as the fallback rather than clobbering it.
    if existing and SB_MCP_TOKEN_RE.match(existing):
        jlog(
            "info",
            "read-token",
            "no SB_READ_TOKEN injected; keeping existing read-only token file",
        )
        return True
    jlog(
        "warn",
        "read-token",
        "no SB_READ_TOKEN injected and no read-token file — leaving pollers on "
        "the rotating admin-token fallback",
    )
    return False


def ensure_admin_token_file(data_dir: str, sb_api: str, mcp_url: str) -> bool:
    """Keep a live admin token at <data_dir>/solarisbay/sb-admin-token (0600).
    The engine's admin toolbox reads it per connection, so a token minted
    here works without a restart. An existing token that still probes OK is
    kept (don't churn SB's token list)."""
    path = os.path.join(data_dir, "solarisbay", "sb-admin-token")
    existing = ""
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read().strip()
    except OSError:
        pass
    if (
        existing
        and SB_MCP_TOKEN_RE.match(existing)
        and probe_admin_token(existing, mcp_url)
    ):
        jlog("info", "admin-mcp", "existing admin token still valid")
        return True
    token = mint_admin_token(sb_api)
    if not token:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        jlog("error", "admin-mcp", "could not write admin token file", error=str(e))
        return False
    jlog("info", "admin-mcp", "wrote admin token file", path=path)
    return True


# ════════════════════════════════════════════════════════════════════════════
# 4. RESTART — last step.
# ════════════════════════════════════════════════════════════════════════════


def restart_solaris(sb_api: str) -> bool:
    """POST /api/services/solaris/action {action: 'restart'} so the chat
    container picks up the patched HASS_TOKEN. Risk-2-safe (#271 spike): SB
    runs this script in an SSH session and the restart is `--no-block` async,
    so the queued restart does not kill the running post-deploy."""
    status, body = post_json(
        f"{sb_api}/api/services/{SOLARIS_SERVICE}/action",
        {"action": "restart"},
        timeout=30,
    )
    if status == 200:
        jlog("info", "restart", "restart requested via ServiceBay API")
        return True
    err = (body or {}).get("error") if isinstance(body, dict) else None
    jlog("warn", "restart", "restart request failed", status=status, error=str(err))
    return False


# ════════════════════════════════════════════════════════════════════════════
# main — the ordered sequence.
# ════════════════════════════════════════════════════════════════════════════


def main() -> int:
    # Converge-only mode (#1023): the self-heal timer re-runs THIS script to
    # re-apply the radicale rights converge if a radicale re-render reverted them.
    # Do just that and exit — none of the deploy-time HA/token/restart wiring.
    if _truthy(env("SOLARIS_CONVERGE_ONLY")):
        converge_radicale_rights()
        return 0

    data_dir = env("DATA_DIR", "/mnt/data")
    sb_api = env("SB_API_URL", "http://localhost:3000").rstrip("/")
    host = env("HOST", "<server-ip>")
    chat_port = env("CHAT_PORT") or chat_container_env("CHAT_PORT") or "8787"
    api_key = env("SOLARIS_API_KEY") or chat_container_env("SOLARIS_API_KEY")
    mcp_url = (
        env("SERVICEBAY_MCP_URL")
        or chat_container_env("SB_MCP_URL")
        or "http://127.0.0.1:5888/mcp"
    )

    # ── 0. voice pipeline containers (#456) ──────────────────────────────────
    # Stand up Solaris's own whisper/Kokoro-TTS/bridge before wiring HA at
    # their endpoints; openWakeWord rides the solaris pod (template.yml).
    install_voice_pipeline(data_dir)

    # ── 1. engine soul ───────────────────────────────────────────────────────
    write_engine_soul(data_dir)

    # ── 2. Home Assistant ────────────────────────────────────────────────────
    ship_alarm_sound(data_dir)
    # Derive the LAN-reachable cast base from the box LAN IP when left empty
    # (#607) — patches the deployed solaris.yml, the restart below picks it up.
    stamp_jellyfin_cast_url(data_dir)
    # Self-heal the Jellyfin `solaris` service-user credential (#626): persist a
    # managed password, reset the lldap user to it, and stamp it into the pod env
    # (the template render zeroed JELLYFIN_PASSWORD → AuthenticateByName 401).
    # The HA jellyfin integration below logs in with the same managed value.
    jellyfin_password = converge_jellyfin_credential(data_dir) or env(
        "JELLYFIN_PASSWORD"
    )
    # Grant the `solaris` DAV account write access to `<resident>/solaris` (option
    # A, #997/#1011): converge Radicale's owner_only → a from_file ruleset that
    # keeps owner_only's guarantee and additionally lets `solaris` write only the
    # per-resident `solaris` calendar. Restarts radicale itself on drift.
    converge_radicale_rights()
    # Durable self-heal (#1023): a radicale re-render/autoupdate reverts the
    # rights to stock owner_only until the next solaris deploy. Install a
    # host-side timer that periodically re-runs this converge so the calendar
    # feature heals without a full re-install.
    install_radicale_rights_timer(data_dir)
    # Route household-wide dated items to the primary resident's calendar (#1011):
    # stamp HOUSEHOLD_CALENDAR_UID (persisted operator choice) into the pod env,
    # which the renderer otherwise prunes. The restart below picks it up.
    apply_household_calendar_uid_to_engine(data_dir)
    # Re-ingest the resident's own written deadlines/tasks (#524, option a): point
    # the CalDAV READ ingest at the same `/<uid>/solaris/` mirror the write path
    # fills, reusing the managed `solaris` DAV password. Renderer prunes these env
    # entries, so we stamp them; the restart below picks them up.
    apply_caldav_read_to_engine(data_dir, jellyfin_password)
    # Make the #931 paperless-ingest handoff run for real (#1034, option A):
    # persist a managed paperless admin password, mint that admin's DRF API token
    # once paperless is reachable, and stamp PAPERLESS_URL + PAPERLESS_TOKEN into
    # the pod env (the renderer prunes empty env). No-op when paperless is absent.
    converge_paperless_credential(data_dir)
    ha_token = adopt_ha_long_lived_token(data_dir)
    if ha_token:
        ensure_ha_jellyfin_integration(
            ha_token,
            env("JELLYFIN_URL"),
            env("JELLYFIN_USERNAME") or JELLYFIN_SOLARIS_USER,
            jellyfin_password,
        )
        wire_voice_pipeline(ha_token, chat_port, api_key, data_dir)

    # ── 3. admin MCP token ───────────────────────────────────────────────────
    ensure_admin_token_file(data_dir, sb_api, mcp_url)
    # The non-expiring read-only token the unattended pollers use so they don't
    # 401-churn when the rotating admin token lapses (servicebay#2317, #818).
    ensure_read_token_file(data_dir)

    # ── 4. restart ───────────────────────────────────────────────────────────
    time.sleep(3)
    restart_solaris(sb_api)

    if api_key:
        emit_credential(
            service="Solaris (Solaris Engine API)",
            url=f"http://{host}:{chat_port}/ollama",
            username="(bearer token)",
            password=api_key,
            importance="critical",
            notes="Bearer for the engine's Ollama-compatible facade (HA conversation agent + gatekeeper). Send as `Authorization: Bearer <key>`.",
        )

    print(f"✅ Solaris is configured: Solaris Engine on port {chat_port}.")
    print("   Chat surface + gatekeeper voice bridge run in the same Pod;")
    print("   the Voice PE rides HA's Assist pipeline into the engine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
