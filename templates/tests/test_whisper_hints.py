"""Per-request word hints for the GPU whisper container (#1157).

#1142 primes whisper on the whole household at deploy time. This is the other
half: the words that matter for THIS utterance, taken off the Wyoming
`Transcribe` (`transcript_names`/`transcript_terms`, already part of wyoming
1.10.0) and appended to that connection's `initial_prompt`.

The stock linuxserver image ignores those fields, so the s6 run script Solaris
already mounts (#1142) now launches a small wrapper module instead of
`python3 -m wyoming_faster_whisper`. The wrapper runs the stock server with one
patch — which means it lives or dies with upstream's shape, so it asserts that
shape at startup and refuses to start when it moved. These tests exercise the
wrapper against a stub of that shape (real files on disk: the assertion reads
`inspect.getsource`).

The same wrapper serves the segment endpoint (foundry-chronicle#141): POST the
path of a recording under the read-only mount, get timestamped segments back.
It shares the household's model, so the gate that keeps an hours-long batch run
from sitting on a three-second voice command is tested here too."""

from __future__ import annotations

import asyncio
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

STUB_MODULES = (
    "solaris_whisper_hints",
    "faster_whisper",
    "wyoming",
    "wyoming.asr",
    "wyoming.event",
    "wyoming_faster_whisper",
    "wyoming_faster_whisper.__main__",
    "wyoming_faster_whisper.dispatch_handler",
    "wyoming_faster_whisper.faster_whisper_handler",
)


@pytest.fixture(scope="module")
def pd():
    path = TEMPLATES / "solaris" / "post-deploy.py"
    spec = importlib.util.spec_from_file_location("solaris_pd_hints", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["solaris_pd_hints"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_stub_modules():
    for name in STUB_MODULES:
        sys.modules.pop(name, None)
    yield
    for name in STUB_MODULES:
        sys.modules.pop(name, None)


def _stub_tree(root, hint_fields=True, prompt_attr="initial_prompt", model_call=True):
    """A minimal on-disk stand-in for the image's wyoming + server packages.

    On disk, not in memory: the wrapper's shape assertion reads the handler with
    `inspect.getsource`, so a class conjured by `exec` would not exercise it."""
    fields = (
        "    transcript_names: Optional[List[str]] = None\n"
        "    transcript_terms: Optional[List[str]] = None\n"
        if hint_fields
        else ""
    )
    parsed = (
        '            transcript_names=data.get("transcript_names"),\n'
        '            transcript_terms=data.get("transcript_terms"),\n'
        if hint_fields
        else ""
    )
    (root / "wyoming").mkdir(parents=True)
    (root / "wyoming" / "__init__.py").write_text("")
    (root / "wyoming" / "event.py").write_text(
        "from dataclasses import dataclass\n"
        "from typing import Any, Dict, Optional\n\n\n"
        "@dataclass\n"
        "class Event:\n"
        "    type: str\n"
        "    data: Optional[Dict[str, Any]] = None\n"
    )
    (root / "wyoming" / "asr.py").write_text(
        "from dataclasses import dataclass\n"
        "from typing import List, Optional  # noqa: F401\n\n\n"
        "@dataclass\n"
        "class Transcribe:\n"
        "    language: Optional[str] = None\n"
        f"{fields}"
        "\n"
        "    @staticmethod\n"
        "    def is_type(event_type):\n"
        '        return event_type == "transcribe"\n\n'
        "    @staticmethod\n"
        "    def from_event(event):\n"
        "        data = event.data or {}\n"
        "        return Transcribe(\n"
        '            language=data.get("language"),\n'
        f"{parsed}"
        "        )\n"
    )
    (root / "wyoming_faster_whisper").mkdir()
    (root / "wyoming_faster_whisper" / "__init__.py").write_text(
        '__version__ = "3.5.0"\n'
    )
    (root / "wyoming_faster_whisper" / "__main__.py").write_text(
        "RAN = []\n\n\ndef run():\n    RAN.append(True)\n"
    )
    (root / "wyoming_faster_whisper" / "dispatch_handler.py").write_text(
        "SEEN = []\n\n\n"
        "class DispatchEventHandler:\n"
        "    def __init__(self, loader):\n"
        "        self._loader = loader\n\n"
        "    async def handle_event(self, event):\n"
        f"        SEEN.append(dict(initial_prompt=self._loader.{prompt_attr}))\n"
        "        return True\n\n"
        "    async def _commit_path(self):\n"
        f"        return dict(initial_prompt=self._loader.{prompt_attr})\n"
    )
    # The token budget mirrors the box: max_length 448 -> 223 tokens, and every
    # whitespace-separated chunk costs 3, the measured cost of a fantasy name.
    (root / "faster_whisper.py").write_text(
        "STEP_HOOK = None\n"
        "CALLS = []\n\n\n"
        "class _Segment:\n"
        "    def __init__(self, start, end, text):\n"
        "        self.start = start\n"
        "        self.end = end\n"
        "        self.text = text\n\n\n"
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
        "    def transcribe(self, audio, language=None, beam_size=1,\n"
        "                   hotwords=None, vad_filter=False, vad_parameters=None):\n"
        "        CALLS.append(dict(audio=audio, language=language,\n"
        "                          beam_size=beam_size, hotwords=hotwords))\n\n"
        "        def steps():\n"
        "            for i in range(3):\n"
        "                if STEP_HOOK is not None:\n"
        "                    STEP_HOOK(i)\n"
        "                yield _Segment(i * 30.0, i * 30.0 + 29.0, f'text {i}')\n\n"
        "        return steps(), dict(duration=90.0)\n"
    )
    call = "self.model.transcribe(" if model_call else "self.model.run("
    (root / "wyoming_faster_whisper" / "faster_whisper_handler.py").write_text(
        "class FasterWhisperTranscriber:\n"
        "    def __init__(self, model):\n"
        "        self.model = model\n"
        "        self.vad_filter = False\n"
        "        self.vad_parameters = None\n\n"
        "    def transcribe(self, wav_path, language, beam_size=1,\n"
        "                   initial_prompt=None):\n"
        f"        segments, _info = {call}wav_path, language=language)\n"
        '        return " ".join(s.text for s in segments)\n'
    )


class _Loader:
    """Stands in for the server's shared ModelLoader."""

    def __init__(self, initial_prompt):
        self.initial_prompt = initial_prompt
        self.beam_size = 1


def _wrapper(pd, tmp_path, monkeypatch, **stub):
    _stub_tree(tmp_path, **stub)
    monkeypatch.syspath_prepend(str(tmp_path))
    path = tmp_path / "solaris_whisper_hints.py"
    path.write_text(pd.WHISPER_HINTS_MODULE)
    spec = importlib.util.spec_from_file_location("solaris_whisper_hints", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["solaris_whisper_hints"] = mod
    spec.loader.exec_module(mod)
    return mod


def _transcribe_event(**data):
    from wyoming.event import Event

    return Event("transcribe", data)


# -- the run script + the mount ----------------------------------------------


def test_run_script_launches_the_hint_wrapper_and_still_parses(pd):
    script = pd.WHISPER_RUN_SCRIPT
    assert "python3 /solaris_whisper_hints.py" in script
    # The wrapper replaces the stock entrypoint; it then runs the stock server
    # itself, so upstream's flags must all still be handed through.
    assert "python3 -m wyoming_faster_whisper" not in script
    for flag in ("--uri", "--model", "--beam-size", "--language", "--data-dir"):
        assert flag in script
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_gpu_unit_mounts_the_hint_wrapper(pd):
    unit = pd.render_whisper_unit("/mnt/data", "medium-int8", "de", gpu=True)
    assert (
        "Volume=/mnt/data/voice/whisper_hints.py:/solaris_whisper_hints.py:ro,Z" in unit
    )


def test_install_whisper_unit_drops_the_wrapper_next_to_the_run_script(
    pd, monkeypatch, tmp_path
):
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    wrapper = tmp_path / "voice" / "whisper_hints.py"
    assert wrapper.read_text() == pd.WHISPER_HINTS_MODULE
    compile(wrapper.read_text(), str(wrapper), "exec")


def test_cpu_path_has_no_wrapper(pd, monkeypatch, tmp_path):
    # The CPU image is a different upstream that takes CLI args directly (#1142)
    # and has no run script to override, so it gets no wrapper.
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    assert not (tmp_path / "voice" / "whisper_hints.py").exists()
    assert "solaris_whisper_hints" not in pd.render_whisper_unit(
        "/mnt/data", "base-int8", "de", gpu=False
    )


# -- the wrapper --------------------------------------------------------------


def test_request_words_append_behind_the_static_prompt(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    loader = _Loader("Sprachbefehle im Haushalt. Die Geräte heißen: Leselicht.")
    handler = DispatchEventHandler(loader)
    asyncio.run(
        handler.handle_event(
            _transcribe_event(
                language="de",
                transcript_names=["Thorgrim"],
                transcript_terms=["Silbermark", "  "],
            )
        )
    )
    # Whisper keeps the LAST 224 tokens, so the request's own words go at the
    # end — the static #1142 lead-in survives, the request's words survive
    # truncation, and blanks never become an empty list entry.
    assert handler._loader.initial_prompt == (
        "Sprachbefehle im Haushalt. Die Geräte heißen: Leselicht. Thorgrim, Silbermark."
    )
    # Everything else is still the one shared loader.
    assert handler._loader.beam_size == 1
    assert loader.initial_prompt.endswith("Leselicht.")


def test_a_request_without_words_is_untouched(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    loader = _Loader("Die Geräte heißen: Leselicht.")
    handler = DispatchEventHandler(loader)
    asyncio.run(handler.handle_event(_transcribe_event(language="de")))
    # Not merely equal — the same object: today's behaviour, byte for byte.
    assert handler._loader is loader


def test_a_second_request_does_not_stack_onto_the_first(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    handler = DispatchEventHandler(_Loader("Geräte: Leselicht."))
    for name in ("Thorgrim", "Yseult"):
        asyncio.run(handler.handle_event(_transcribe_event(transcript_names=[name])))
    assert handler._loader.initial_prompt == "Geräte: Leselicht. Yseult."


def test_startup_names_the_version_it_matched(pd, tmp_path, monkeypatch, capsys):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    line = capsys.readouterr().err
    assert "solaris-hints: active on wyoming-faster-whisper 3.5.0" in line
    from wyoming_faster_whisper.__main__ import RAN

    assert RAN == [True]


def test_it_refuses_to_start_when_the_prompt_site_moved(
    pd, tmp_path, monkeypatch, capsys
):
    # An unattended linuxserver bump (AutoUpdate=registry) can restructure the
    # module this patch reaches into. Failing to start makes systemd show the
    # container down; silently serving without the words would not.
    mod = _wrapper(pd, tmp_path, monkeypatch, prompt_attr="prompt")
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "solaris-hints: NOT APPLIED" in err
    assert "expected 2" in err
    from wyoming_faster_whisper.__main__ import RAN

    assert RAN == []


def test_it_refuses_to_start_when_transcribe_lost_the_hint_fields(
    pd, tmp_path, monkeypatch, capsys
):
    mod = _wrapper(pd, tmp_path, monkeypatch, hint_fields=False)
    assert mod.main() == 1
    assert "transcript_names" in capsys.readouterr().err
    from wyoming_faster_whisper.__main__ import RAN

    assert RAN == []


def test_it_refuses_to_start_when_the_model_call_moved(
    pd, tmp_path, monkeypatch, capsys
):
    # The gate serialises the model by wrapping FasterWhisperTranscriber.transcribe.
    # If that is no longer where the model is called, the wrap guards nothing and
    # a batch run could hold the GPU against the household unnoticed.
    mod = _wrapper(pd, tmp_path, monkeypatch, model_call=False)
    assert mod.main() == 1
    assert "no longer calls self.model.transcribe(" in capsys.readouterr().err


# -- the segment endpoint (#1157, foundry-chronicle#141) ----------------------


def _serve(pd, tmp_path, monkeypatch):
    """The wrapper with a recordings mount, its endpoint listening."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "kai.wav").write_bytes(b"RIFF....WAVE")
    monkeypatch.setenv("WHISPER_SEGMENTS_ROOT", str(recordings))
    # A real port, not 0: the caller needs a fixed one, so the wrapper treats
    # port 0 as "no endpoint" rather than as an ephemeral bind.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        monkeypatch.setenv("WHISPER_SEGMENTS_PORT", str(probe.getsockname()[1]))
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    server = mod._loaded["server"]
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


def test_endpoint_returns_timestamped_segments(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    import faster_whisper

    model = faster_whisper.WhisperModel()
    from wyoming_faster_whisper.faster_whisper_handler import FasterWhisperTranscriber

    FasterWhisperTranscriber(model)  # the patched __init__ registers it
    status, body = _post(port, {"path": str(recordings / "kai.wav"), "language": "de"})
    assert status == 200
    # start, end, text — nothing else: foundry-chronicle stores exactly these
    # three and converts the seconds to their own ms columns.
    assert body["segments"] == [
        {"start": 0.0, "end": 29.0, "text": "text 0"},
        {"start": 30.0, "end": 59.0, "text": "text 1"},
        {"start": 60.0, "end": 89.0, "text": "text 2"},
    ]
    assert faster_whisper.CALLS[-1]["language"] == "de"
    mod._loaded["server"].shutdown()


def test_endpoint_refuses_a_path_outside_the_mount(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"x")
    for candidate in ("../secret.wav", str(outside), "/etc/passwd", ""):
        status, body = _post(port, {"path": candidate})
        assert status == 403, candidate
        assert "must be a file under" in body["error"]
    mod._loaded["server"].shutdown()


def test_over_budget_hints_are_reported_not_silently_dropped(pd, tmp_path, monkeypatch):
    # The whole point: 52 real names measure 415 tokens against a budget of 223,
    # and faster-whisper would cut the token list mid-name and say nothing. They
    # only notice weeks later, by which time the recording is deleted.
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    import faster_whisper
    from wyoming_faster_whisper.faster_whisper_handler import FasterWhisperTranscriber

    FasterWhisperTranscriber(faster_whisper.WhisperModel())
    names = [f"Name{i}" for i in range(80)]
    status, body = _post(port, {"path": str(recordings / "kai.wav"), "hotwords": names})
    assert status == 200
    # 3 tokens per comma-joined name, budget 223 -> 74 fit, the rest are named.
    assert body["hotwords_dropped_count"] == 6
    assert body["hotwords_dropped"] == names[74:]
    used = faster_whisper.CALLS[-1]["hotwords"]
    assert used.startswith("Name0, Name1")
    assert "Name74" not in used
    mod._loaded["server"].shutdown()


def test_no_hints_leaves_the_hotwords_unset(pd, tmp_path, monkeypatch):
    mod, recordings, port = _serve(pd, tmp_path, monkeypatch)
    import faster_whisper
    from wyoming_faster_whisper.faster_whisper_handler import FasterWhisperTranscriber

    FasterWhisperTranscriber(faster_whisper.WhisperModel())
    status, body = _post(port, {"path": str(recordings / "kai.wav")})
    assert status == 200
    assert body["hotwords_dropped"] == []
    assert faster_whisper.CALLS[-1]["hotwords"] is None
    mod._loaded["server"].shutdown()


def test_there_is_no_endpoint_without_a_recordings_mount(pd, tmp_path, monkeypatch):
    # Every other box: no mount, no listener, no new surface.
    monkeypatch.delenv("WHISPER_SEGMENTS_ROOT", raising=False)
    monkeypatch.delenv("WHISPER_SEGMENTS_PORT", raising=False)
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    assert mod.serve_segments() is None
    assert "server" not in mod._loaded


def test_the_gate_opens_between_segments_and_the_household_goes_first(
    pd, tmp_path, monkeypatch
):
    # An hours-long batch run must not hold the model against a 3s voice command.
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    import faster_whisper
    from wyoming_faster_whisper.faster_whisper_handler import FasterWhisperTranscriber

    transcriber = FasterWhisperTranscriber(faster_whisper.WhisperModel())
    order = []

    def household():
        with mod.GATE.hold(household=True):
            order.append("household")

    waiter = []

    def hook(index):
        order.append(f"segment{index}")
        if index == 0:
            thread = threading.Thread(target=household)
            waiter.append(thread)
            thread.start()
            # Block until it is genuinely queued on the gate, so the assertion
            # below is about priority and not about thread scheduling luck.
            while mod.GATE._household_waiting == 0:
                time.sleep(0.001)

    monkeypatch.setattr(faster_whisper, "STEP_HOOK", hook)
    segments = mod.transcribe_file(transcriber, "kai.wav", "de", [])
    waiter[0].join(timeout=5)
    assert len(segments) == 3
    # The household request is served BETWEEN two segments, not after the job.
    assert order == ["segment0", "household", "segment1", "segment2"]


def test_the_household_path_holds_the_gate(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.faster_whisper_handler import FasterWhisperTranscriber

    assert FasterWhisperTranscriber.transcribe is mod.household_transcribe
