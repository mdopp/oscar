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
import time
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
    # The GPU worker is a real child process, so a test that started one leaves
    # it running unless it is reaped here.
    mod = sys.modules.get("solaris_whisper_batch")
    if mod is not None and hasattr(mod, "stop_worker"):
        mod.stop_worker()
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
    # The card is borrowed per job, not held for the day (#1259).
    assert "Environment=WHISPER_BATCH_IDLE_S=300" in unit
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


def _stub_faster_whisper(
    root, hotwords=True, vad=True, segment_fields=("start", "end", "text")
):
    """A minimal on-disk stand-in for faster-whisper.

    On disk, not in memory: the startup assertion reads the model class with
    `inspect.getsource`, so a class conjured by `exec` would not exercise it.
    The token budget mirrors the box — max_length 448 → 223 tokens, and every
    whitespace-separated chunk costs 3, the measured cost of a fantasy name.
    A stubbed run is 90 s of audio of which 60 s are silence."""
    fields = "".join(f"    {name}: float\n" for name in segment_fields)
    signature = "audio, language=None, beam_size=1"
    if hotwords:
        signature += ", hotwords=None"
    if vad:
        signature += ", vad_filter=False, vad_parameters=None"
    (root / "faster_whisper").mkdir(parents=True)
    (root / "faster_whisper" / "__init__.py").write_text(
        "from faster_whisper.transcribe import (  # noqa: F401\n"
        "    Segment,\n"
        "    TranscriptionInfo,\n"
        "    WhisperModel,\n"
        ")\n\n"
        '__version__ = "1.2.1"\n\n\n'
        "def download_model(size_or_id, cache_dir=None, **kwargs):\n"
        "    return cache_dir or size_or_id\n"
    )
    (root / "faster_whisper" / "transcribe.py").write_text(
        "import dataclasses\n"
        "import json\n"
        "import os\n\n"
        "CALLS = []\n\n\n"
        "@dataclasses.dataclass\n"
        "class Segment:\n"
        f"{fields}"
        "\n\n"
        "@dataclasses.dataclass\n"
        "class TranscriptionInfo:\n"
        "    duration: float\n"
        "    duration_after_vad: float\n\n\n"
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
        "        filtered = bool(locals().get('vad_filter'))\n"
        "        call = dict(audio=audio, language=language,\n"
        "                    beam_size=beam_size,\n"
        "                    hotwords=locals().get('hotwords'),\n"
        "                    vad_filter=locals().get('vad_filter'),\n"
        "                    vad_parameters=locals().get('vad_parameters'))\n"
        "        CALLS.append(call)\n"
        # The model runs in a worker CHILD process, so the test's own CALLS list
        # never sees a call — the child appends it to a file instead.
        "        record = os.environ.get('FW_STUB_CALLS')\n"
        "        if record:\n"
        "            with open(record, 'a') as fh:\n"
        "                fh.write(json.dumps(call) + '\\n')\n"
        "        segments = [Segment(i * 30.0, i * 30.0 + 29.0, 'text %d' % i)\n"
        "                    for i in range(3)]\n"
        "        return iter(segments), TranscriptionInfo(90.0,\n"
        "                                                30.0 if filtered else 90.0)\n"
    )


def _module(pd, tmp_path, monkeypatch, **stub):
    _stub_faster_whisper(tmp_path, **stub)
    monkeypatch.syspath_prepend(str(tmp_path))
    # The worker is a real child process (that is the whole of #1259): it needs
    # the stub on its own import path, and a file to record its calls in.
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("FW_STUB_CALLS", str(tmp_path / "calls.jsonl"))
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
    server = mod.serve()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return mod, recordings, server.server_address[1]


def _calls(tmp_path):
    """What the worker child actually asked faster-whisper for."""
    path = tmp_path / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


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
    assert _calls(tmp_path)[-1]["language"] == "de"
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
    used = _calls(tmp_path)[-1]["hotwords"]
    assert used.startswith("Name0, Name1")
    assert "Name74" not in used
    mod._loaded["server"].shutdown()


def test_no_hotwords_leaves_them_unset(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(port, {"path": str(recordings / "chunk-02.wav")})
    assert status == 200
    assert body["hotwords_dropped"] == []
    assert _calls(tmp_path)[-1]["hotwords"] is None
    mod._loaded["server"].shutdown()


# -- silence must not be decoded into the name register (#1204) ---------------
#
# On a per-speaker track most of a session is the other four talking, and
# hotwords prime the decoder to emit the round's register when it hears nothing.
# The caller cannot tell an invented name from a spoken one — only this side
# sees the audio. Box-measured on 60 s of digital silence with 50 hotwords: two
# invented segments without the filter, none with it.


def test_silence_detection_is_on_for_a_caller_that_asks_for_nothing(
    pd, tmp_path, monkeypatch
):
    # foundry-chronicle was written before this field existed and sends no
    # `vad`; the default is what actually fixes its transcripts.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(port, {"path": str(recordings / "chunk-02.wav")})
    assert status == 200
    assert body["vad"] is True
    assert _calls(tmp_path)[-1]["vad_filter"] is True
    assert _calls(tmp_path)[-1]["vad_parameters"] == mod.VAD_PARAMETERS
    mod._loaded["server"].shutdown()


def test_a_caller_can_switch_the_silence_detection_off(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(port, {"path": str(recordings / "chunk-02.wav"), "vad": False})
    assert status == 200
    assert body["vad"] is False
    assert body["silence_dropped_seconds"] == 0.0
    assert _calls(tmp_path)[-1]["vad_filter"] is False
    mod._loaded["server"].shutdown()


def test_the_response_says_how_much_audio_was_discarded_as_silence(
    pd, tmp_path, monkeypatch
):
    # The point of the field: without a number the caller has to trust that the
    # filter ran at all. 90 s in, 30 s of speech out ⇒ 60 s discarded.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    status, body = _post(port, {"path": str(recordings / "chunk-02.wav")})
    assert status == 200
    assert body["audio_seconds"] == 90.0
    assert body["silence_dropped_seconds"] == 60.0
    # Additive only — foundry-chronicle is live against this response shape.
    assert body["hotwords_dropped_count"] == 0 and body["hotwords_dropped"] == []
    assert body["segments"][0] == {"start": 0.0, "end": 29.0, "text": "text 0"}
    mod._loaded["server"].shutdown()


def test_the_vad_thresholds_stay_at_the_values_measured_for_a_table(
    pd, tmp_path, monkeypatch
):
    # The opposite failure is worse than the one being fixed: a tighter VAD
    # clips the quiet start of a sentence at a table of six. Same reasoning as
    # the household unit's (#1158), pinned here because the response hands the
    # caller a figure these values decide and the image auto-updates.
    mod = _module(pd, tmp_path, monkeypatch)
    assert mod.VAD_PARAMETERS == {
        "threshold": 0.5,
        "min_silence_duration_ms": 2000,
        "speech_pad_ms": 400,
    }


def test_it_refuses_to_start_when_the_silence_filter_left_the_model(
    pd, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("WHISPER_BATCH_PORT", "10301")
    mod = _module(pd, tmp_path, monkeypatch, vad=False)
    assert mod.main() == 1
    assert "takes no vad_filter" in capsys.readouterr().err


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


def test_the_endpoint_listens_on_loopback_only(pd, tmp_path, monkeypatch):
    # The container runs on the host network, so a wildcard bind hands the
    # whole LAN an endpoint that transcribes the household's session
    # recordings. Box-observed once the unit was finally running: `ss -lntp`
    # showed `0.0.0.0:10301` next to whisper's `127.0.0.1:10300`.
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("WHISPER_BATCH_PORT", "0")
    mod = _module(pd, tmp_path, monkeypatch)
    server = mod.serve()
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_a_healthy_start_serves_without_taking_the_card(
    pd, tmp_path, monkeypatch, capsys
):
    # #1259: the main process holds no model, so an idle container holds no
    # VRAM. The model cache is still filled before the port binds — on the CPU,
    # inside TimeoutStartSec — so a first start absorbs the ~1.6 GB download
    # rather than putting it inside the first caller's request.
    monkeypatch.setenv("WHISPER_BATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("WHISPER_BATCH_PORT", "10301")
    mod = _module(pd, tmp_path, monkeypatch)
    served = []

    class _Server:
        def serve_forever(self):
            served.append(mod._worker["proc"])

    monkeypatch.setattr(mod, "serve", _Server)
    assert mod.main() == 0
    assert served == [None]
    err = capsys.readouterr().err
    assert "faster-whisper 1.2.1, large-v3-turbo/float16 on cuda per job" in err
    assert "idle release after 300s" in err


# -- the card is borrowed, not held (#1259) -----------------------------------
#
# 2216 MiB held around the clock for a service used for an hour a week is what
# decides whether the household chat model stays resident: freeing it takes the
# box's available VRAM from ~11.2 to ~13.4 GiB. The model therefore lives in a
# worker CHILD process — a process exit is the only thing that gives the memory
# back, because CTranslate2's CUDA allocator caches every block it frees.


def test_an_idle_container_holds_no_gpu(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    # Nothing on the card until a job arrives.
    assert mod._worker["proc"] is None
    status, _ = _post(port, {"path": str(recordings / "chunk-02.wav")})
    assert status == 200
    worker = mod._worker["proc"]
    assert worker is not None and worker.poll() is None
    # Still busy a moment ago ⇒ the worker stays, so the chunks of one session
    # share it.
    assert mod.reap_idle_worker() is False
    assert worker.poll() is None
    mod._worker["used"] = time.monotonic() - mod.IDLE_S - 1
    assert mod.reap_idle_worker() is True
    assert mod._worker["proc"] is None
    # GONE, not merely unreferenced: that is what the driver counts.
    assert worker.wait(timeout=30) is not None
    mod._loaded["server"].shutdown()


def test_the_first_job_after_an_idle_release_still_transcribes(
    pd, tmp_path, monkeypatch
):
    # The cold start is the whole cost of this trade: the first chunk after an
    # idle period reloads the model. It must be a slower answer, never a wrong
    # one and never a 5xx.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    payload = {"path": str(recordings / "chunk-02.wav"), "language": "de"}
    first = _post(port, payload)
    mod._worker["used"] = time.monotonic() - mod.IDLE_S - 1
    assert mod.reap_idle_worker() is True
    second = _post(port, payload)
    assert second[0] == 200
    assert second == first
    assert second[1]["segments"][0] == {"start": 0.0, "end": 29.0, "text": "text 0"}
    # Both jobs reached a real model, and the second took the card back.
    assert len(_calls(tmp_path)) == 2
    assert mod._worker["proc"] is not None and mod._worker["proc"].poll() is None
    mod._loaded["server"].shutdown()


def test_a_session_of_chunks_pays_the_cold_start_once(pd, tmp_path, monkeypatch):
    # A caller splits a session into 10-30 min chunks and submits them back to
    # back; reloading per chunk would trade the headroom for the speed.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    payload = {"path": str(recordings / "chunk-02.wav")}
    assert _post(port, payload)[0] == 200
    pid = mod._worker["proc"].pid
    assert _post(port, payload)[0] == 200
    assert mod._worker["proc"].pid == pid
    assert len(_calls(tmp_path)) == 2
    mod._loaded["server"].shutdown()
