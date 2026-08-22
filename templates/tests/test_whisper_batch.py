"""The second whisper container: timestamped batch transcription (#1161).

`solaris-whisper-batch` is a separate container with a separate model on the
same card — not a second model inside the household STT process. The shared
model attempt (#1159, reverted by #1160) cost the household probe 133s; two
CUDA processes time-slice at kernel granularity and cost it ~3s.

These tests cover the two halves: the Quadlet the post-deploy writes (and
un-writes), and the service module it mounts — path confinement, the hotword
budget report, the startup shape assertion, and the timestamp contract, which
is the one thing here that can break silently."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]

STUB_MODULES = ("solaris_whisper_batch", "faster_whisper", "faster_whisper.transcribe")


@pytest.fixture(scope="module")
def pd():
    path = TEMPLATES / "solaris" / "post-deploy.py"
    spec = importlib.util.spec_from_file_location("solaris_pd_batch", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["solaris_pd_batch"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_stub_modules():
    for name in STUB_MODULES:
        sys.modules.pop(name, None)
    yield
    for name in STUB_MODULES:
        sys.modules.pop(name, None)


# -- the Quadlet --------------------------------------------------------------


def test_batch_unit_is_a_gpu_container_on_the_stock_image(pd):
    unit = pd.render_whisper_batch_unit("/mnt/data", "/mnt/data/stacks/rec", 3)
    # No new GHCR image: the stock :gpu base already carries faster-whisper,
    # CTranslate2 and CUDA, so this changes nothing in the release chain.
    assert "Image=lscr.io/linuxserver/faster-whisper:gpu" in unit
    assert "ContainerName=solaris-whisper-batch" in unit
    assert "AddDevice=nvidia.com/gpu=all" in unit
    assert "SecurityLabelDisable=true" in unit
    # Host network on both containers, and only this one binds :10301.
    assert "Network=host" in unit
    assert "Environment=WHISPER_BATCH_PORT=10301" in unit
    assert "Environment=WHISPER_BATCH_MODEL=large-v3-turbo" in unit
    assert "Environment=WHISPER_BATCH_COMPUTE=float16" in unit
    # Its own model cache, never the household unit's whisper-gpu dir.
    assert "Volume=/mnt/data/voice/whisper-batch:/config:Z" in unit
    assert "whisper-gpu" not in unit
    # The 50% CPU guardrail. Measured need is 1.0 of 6 cores, so it does not
    # bind — it is here against a model that behaves differently.
    assert "PodmanArgs=--cpus 3" in unit


def test_batch_unit_mounts_the_recordings_read_only_at_the_same_path(pd):
    root = "/mnt/data/stacks/daggerheart-aufnahmen"
    unit = pd.render_whisper_batch_unit("/mnt/data", root, 3)
    # Same path inside as outside, so the caller sends the path it knows; :ro
    # and no :Z — relabelling another stack's data dir would break its owner.
    assert f"Volume={root}:{root}:ro\n" in unit
    assert f"{root}:{root}:ro,Z" not in unit
    assert f"Environment=WHISPER_BATCH_ROOT={root}" in unit


def test_batch_unit_runs_the_endpoint_instead_of_the_wyoming_server(pd):
    unit = pd.render_whisper_batch_unit("/mnt/data", "/rec", 3)
    # Our run script REPLACES the image's whisper service, so this container
    # never starts a Wyoming server and cannot contend for :10300 on host net.
    assert (
        "Volume=/mnt/data/voice/whisper-batch-run:"
        "/etc/s6-overlay/s6-rc.d/svc-whisper/run:ro,Z" in unit
    )
    assert (
        "Volume=/mnt/data/voice/whisper_batch.py:/solaris_whisper_batch.py:ro,Z" in unit
    )
    assert "10300" not in unit


def test_batch_run_script_derives_the_cuda_library_path(pd):
    script = pd.WHISPER_BATCH_RUN_SCRIPT
    # Without this the model loads into VRAM and the FIRST encode dies with
    # "Library libcublas.so.12 is not found" (#1162) — a container that looks
    # healthy until a real file arrives.
    assert pd.WHISPER_CUDA_LIB_PREAMBLE in script
    assert pd.WHISPER_CUDA_LIB_PREAMBLE in pd.WHISPER_RUN_SCRIPT
    assert 'export LD_LIBRARY_PATH="${cuda_lib_path}' in script
    assert "libcublas.so.12" in script and "libcudnn.so.9" in script
    assert "python3 /solaris_whisper_batch.py" in script
    assert "wyoming_faster_whisper" not in script
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


# -- installing and un-installing it ------------------------------------------


def _install(pd, monkeypatch, tmp_path, enabled="true", gpu=True, recordings=True):
    root = tmp_path / "recordings"
    if recordings:
        root.mkdir(exist_ok=True)
    installed, removed = {}, []
    monkeypatch.setattr(pd, "cdi_available", lambda: gpu)
    monkeypatch.setattr(
        pd,
        "env",
        lambda key, default="": {
            "WHISPER_BATCH_ENABLED": enabled,
            "WHISPER_BATCH_ROOT": str(root),
        }.get(key, default),
    )
    monkeypatch.setattr(
        pd,
        "install_unit",
        lambda unit, content: installed.update(unit=unit, content=content) or True,
    )
    monkeypatch.setattr(pd, "remove_unit", lambda unit: removed.append(unit) or True)
    result = pd.install_whisper_batch_unit(str(tmp_path))
    return result, installed, removed


def test_it_installs_only_when_enabled_and_the_recordings_are_there(
    pd, monkeypatch, tmp_path
):
    result, installed, removed = _install(pd, monkeypatch, tmp_path)
    assert result is True
    assert installed["unit"] == "solaris-whisper-batch"
    assert removed == []
    # The mounted script and the module land next to the cache dir before the
    # unit starts — the run script execs the module, so a missing copy is a
    # startup failure, not a degraded feature.
    assert (tmp_path / "voice" / "whisper-batch").is_dir()
    assert (tmp_path / "voice" / "whisper-batch-run").read_text() == (
        pd.WHISPER_BATCH_RUN_SCRIPT
    )
    module = tmp_path / "voice" / "whisper_batch.py"
    assert module.read_text() == pd.WHISPER_BATCH_MODULE
    compile(module.read_text(), str(module), "exec")


@pytest.mark.parametrize(
    "case", [dict(enabled="false"), dict(gpu=False), dict(recordings=False)]
)
def test_every_no_removes_the_unit_rather_than_skipping_it(
    pd, monkeypatch, tmp_path, case
):
    # "Temporary" has to mean temporary: switching the toggle back off takes the
    # container off the box, it does not merely leave it out of the next install.
    result, installed, removed = _install(pd, monkeypatch, tmp_path, **case)
    assert result is False
    assert installed == {}
    assert removed == ["solaris-whisper-batch"]
    assert not (tmp_path / "voice" / "whisper_batch.py").exists()


def test_remove_unit_stops_before_it_deletes(pd, monkeypatch, tmp_path):
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    unit_path = systemd_dir / "solaris-whisper-batch.container"
    unit_path.write_text("UNIT")
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: str(systemd_dir) if "systemd" in p else p
    )
    calls = []

    def _run(args, **kwargs):
        calls.append((list(args), unit_path.exists()))

        class _OK:
            returncode = 0
            stderr = ""

        return _OK()

    monkeypatch.setattr(pd.subprocess, "run", _run)
    assert pd.remove_unit("solaris-whisper-batch") is True
    assert not unit_path.exists()
    # Stop first, while systemd still knows the unit; reload after. The other
    # order leaves the container running until the next reboot.
    assert calls[0][0][:4] == [
        "systemctl",
        "--user",
        "stop",
        "solaris-whisper-batch.service",
    ]
    assert calls[0][1] is True
    assert calls[-1][0] == ["systemctl", "--user", "daemon-reload"]


def test_remove_unit_is_a_noop_when_there_is_nothing_to_remove(
    pd, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: str(tmp_path) if "systemd" in p else p
    )
    monkeypatch.setattr(pd, "service_active", lambda unit: False)
    monkeypatch.setattr(
        pd.subprocess, "run", lambda *a, **k: pytest.fail("nothing to stop")
    )
    assert pd.remove_unit("solaris-whisper-batch") is True


def test_the_toggle_is_an_operator_variable_defaulting_to_unchanged(pd):
    variables = json.loads(
        (TEMPLATES / "solaris" / "variables.json").read_text(encoding="utf-8")
    )
    toggle = variables["WHISPER_BATCH_ENABLED"]
    # Not `false`: ServiceBay keeps no per-template variable, so a default that
    # means "off" is re-applied by every later deploy (#1200).
    assert toggle["default"] == "unchanged"
    assert sorted(toggle["options"]) == ["false", "true", "unchanged"]


def test_a_yes_survives_the_next_deploy_that_passes_no_variable(
    pd, monkeypatch, tmp_path
):
    # The regression that took the accepted #1161 container off the box: the
    # install that turns it on carries the variable, every deploy after it does
    # not — and the "no" path removes the unit rather than skipping it.
    result, installed, removed = _install(pd, monkeypatch, tmp_path)
    assert result is True and removed == []
    result, installed, removed = _install(
        pd, monkeypatch, tmp_path, enabled="unchanged"
    )
    assert result is True
    assert installed["unit"] == "solaris-whisper-batch"
    assert removed == []


def test_an_explicit_no_is_remembered_too(pd, monkeypatch, tmp_path):
    _install(pd, monkeypatch, tmp_path)
    result, _, removed = _install(pd, monkeypatch, tmp_path, enabled="false")
    assert result is False and removed == ["solaris-whisper-batch"]
    result, installed, removed = _install(
        pd, monkeypatch, tmp_path, enabled="unchanged"
    )
    assert result is False and installed == {}
    assert removed == ["solaris-whisper-batch"]


def test_an_unset_box_stays_off(pd, monkeypatch, tmp_path):
    result, installed, removed = _install(
        pd, monkeypatch, tmp_path, enabled="unchanged"
    )
    assert result is False and installed == {}
    assert removed == ["solaris-whisper-batch"]


# -- the service module -------------------------------------------------------


def _stub_faster_whisper(root, hotwords=True, segment_fields=("start", "end", "text")):
    """A minimal on-disk stand-in for faster-whisper.

    On disk, not in memory: the startup assertion reads the model class with
    `inspect.getsource`, so a class conjured by `exec` would not exercise it.
    The token budget mirrors the box — max_length 448 → 223 tokens, and every
    whitespace-separated chunk costs 3, the measured cost of a fantasy name."""
    fields = "".join(f"    {name}: float\n" for name in segment_fields)
    signature = (
        "audio, language=None, beam_size=1, hotwords=None"
        if hotwords
        else "audio, language=None, beam_size=1"
    )
    (root / "faster_whisper").mkdir(parents=True)
    (root / "faster_whisper" / "__init__.py").write_text(
        "from faster_whisper.transcribe import Segment, WhisperModel  # noqa: F401\n\n"
        '__version__ = "1.2.1"\n'
    )
    (root / "faster_whisper" / "transcribe.py").write_text(
        "import dataclasses\n\n"
        "CALLS = []\n\n\n"
        "@dataclasses.dataclass\n"
        "class Segment:\n"
        f"{fields}"
        "\n\n"
        "class _Encoding:\n"
        "    def __init__(self, ids):\n"
        "        self.ids = ids\n\n\n"
        "class HfTokenizer:\n"
        "    def encode(self, text, add_special_tokens=True):\n"
        "        return _Encoding([0] * 3 * len(text.split()))\n\n\n"
        "class WhisperModel:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.max_length = 448\n"
        "        self.hf_tokenizer = HfTokenizer()\n\n"
        f"    def transcribe(self, {signature}):\n"
        "        CALLS.append(dict(audio=audio, language=language,\n"
        "                          beam_size=beam_size,\n"
        "                          hotwords=locals().get('hotwords')))\n"
        "        segments = [Segment(i * 30.0, i * 30.0 + 29.0, 'text %d' % i)\n"
        "                    for i in range(3)]\n"
        "        return iter(segments), dict(duration=90.0)\n"
    )


def _module(pd, tmp_path, monkeypatch, **stub):
    _stub_faster_whisper(tmp_path, **stub)
    monkeypatch.syspath_prepend(str(tmp_path))
    path = tmp_path / "solaris_whisper_batch.py"
    path.write_text(pd.WHISPER_BATCH_MODULE)
    spec = importlib.util.spec_from_file_location("solaris_whisper_batch", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["solaris_whisper_batch"] = mod
    spec.loader.exec_module(mod)
    return mod


def _serve(pd, tmp_path, monkeypatch, **stub):
    """The module with a recordings mount, its endpoint listening."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "chunk-02.wav").write_bytes(b"RIFF....WAVE")
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(recordings))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        monkeypatch.setenv("WHISPER_BATCH_PORT", str(probe.getsockname()[1]))
    mod = _module(pd, tmp_path, monkeypatch, **stub)
    import faster_whisper

    mod._loaded["model"] = faster_whisper.WhisperModel()
    server = mod.serve()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return mod, recordings, server.server_address[1]


def _post(port, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/transcribe",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_segments_are_timed_relative_to_the_submitted_file(pd, tmp_path, monkeypatch):
    # THE contract. The service is stateless: it knows nothing of where this
    # chunk sits in the session, so it must not invent an offset — the caller
    # splits into 10-30 min chunks and adds the offset itself. Get this wrong
    # and all five speaker tracks stack at the session start, which is only
    # noticed once the recording is seven days gone.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(
        port, {"path": str(recordings / "chunk-02.wav"), "language": "de"}
    )
    assert status == 200
    starts = [s["start"] for s in body["segments"]]
    assert starts[0] < 1.0
    assert starts == sorted(starts)
    # start, end, text — nothing else: foundry-chronicle stores exactly these
    # three and converts the seconds into its own ms columns.
    assert body["segments"][0] == {"start": 0.0, "end": 29.0, "text": "text 0"}
    assert all(set(s) == {"start", "end", "text"} for s in body["segments"])
    import faster_whisper.transcribe as stub

    assert stub.CALLS[-1]["language"] == "de"
    mod._loaded["server"].shutdown()


@pytest.mark.parametrize("attack", ["absolute", "traversal", "symlink"])
def test_a_path_out_of_the_mount_is_refused(pd, tmp_path, monkeypatch, attack):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"x")
    if attack == "symlink":
        # A symlink INSIDE the mount pointing out of it: realpath resolves it
        # before the prefix check, so it is refused rather than followed.
        (recordings / "escape.wav").symlink_to(outside)
        candidate = "escape.wav"
    else:
        candidate = str(outside) if attack == "absolute" else "../secret.wav"
    status, body = _post(port, {"path": candidate})
    assert status == 403
    assert "must be a file under" in body["error"]
    mod._loaded["server"].shutdown()


def test_over_budget_hotwords_are_reported_not_silently_dropped(
    pd, tmp_path, monkeypatch
):
    # 52 real names measure 415 tokens against a budget of 223, and
    # faster-whisper would cut the token list mid-name and say nothing.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    names = [f"Name{i}" for i in range(80)]
    status, body = _post(
        port, {"path": str(recordings / "chunk-02.wav"), "hotwords": names}
    )
    assert status == 200
    # 3 tokens per comma-joined name, budget 223 → 74 fit, the rest are NAMED.
    assert body["hotwords_dropped_count"] == 6
    assert body["hotwords_dropped"] == names[74:]
    import faster_whisper.transcribe as stub

    used = stub.CALLS[-1]["hotwords"]
    assert used.startswith("Name0, Name1")
    assert "Name74" not in used
    mod._loaded["server"].shutdown()


def test_no_hotwords_leaves_them_unset(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(port, {"path": str(recordings / "chunk-02.wav")})
    assert status == 200
    assert body["hotwords_dropped"] == []
    import faster_whisper.transcribe as stub

    assert stub.CALLS[-1]["hotwords"] is None
    mod._loaded["server"].shutdown()


def test_the_access_log_names_no_path_and_no_transcript(
    pd, tmp_path, monkeypatch, capsys
):
    # The path is a speaker's name and the text is their session. Those tracks
    # are deleted after 7 days; a log line would outlive them.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(port, {"path": str(recordings / "chunk-02.wav")})
    assert status == 200
    mod._loaded["server"].shutdown()
    err = capsys.readouterr().err
    assert "POST /transcribe" in err
    assert "chunk-02.wav" not in err
    assert body["segments"][0]["text"] not in err


def test_it_refuses_to_start_when_hotwords_left_the_model(pd, tmp_path, monkeypatch):
    # AutoUpdate=registry: faster-whisper moves under us unattended. A silent
    # no-op here would break a second project's transcript, not just a prompt.
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("WHISPER_BATCH_PORT", "10301")
    mod = _module(pd, tmp_path, monkeypatch, hotwords=False)
    assert mod.main() == 1


def test_it_refuses_to_start_when_the_segment_shape_moved(
    pd, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("WHISPER_BATCH_PORT", "10301")
    mod = _module(pd, tmp_path, monkeypatch, segment_fields=("begin", "end", "text"))
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "NOT APPLIED to faster-whisper 1.2.1" in err
    assert "start/end/text" in err


def test_it_refuses_to_listen_without_a_recordings_root(pd, tmp_path, monkeypatch):
    # A listener with nothing it may read would 403 every request; better to
    # fail the container than to look healthy.
    monkeypatch.delenv("WHISPER_BATCH_ROOT", raising=False)
    monkeypatch.delenv("WHISPER_BATCH_PORT", raising=False)
    mod = _module(pd, tmp_path, monkeypatch)
    assert mod.main() == 1


def test_a_healthy_start_loads_the_model_on_cuda_and_serves(
    pd, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("WHISPER_BATCH_PORT", "10301")
    mod = _module(pd, tmp_path, monkeypatch)
    served = []

    class _Server:
        def serve_forever(self):
            # The model is on the card BEFORE the port binds: the s6 readiness
            # check is "nc -z :10301", so a caller never meets a half-load.
            served.append(mod._loaded["model"])

    monkeypatch.setattr(mod, "serve", _Server)
    assert mod.main() == 0
    assert len(served) == 1
    assert (
        "faster-whisper 1.2.1, large-v3-turbo/float16 on cuda"
        in capsys.readouterr().err
    )
