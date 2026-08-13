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
`inspect.getsource`)."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import subprocess
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]

STUB_MODULES = (
    "solaris_whisper_hints",
    "wyoming",
    "wyoming.asr",
    "wyoming.event",
    "wyoming_faster_whisper",
    "wyoming_faster_whisper.__main__",
    "wyoming_faster_whisper.dispatch_handler",
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


def _stub_tree(root, hint_fields=True, prompt_attr="initial_prompt"):
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
