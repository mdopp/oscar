"""Per-request word hints for the GPU whisper container (#1157).

#1142 primes whisper on the whole household at deploy time. This is the other
half: the words that matter for THIS utterance, taken off the Wyoming
`Transcribe` (`transcript_names`/`transcript_terms`, already part of wyoming
1.10.0) and appended to that connection's `initial_prompt`.

The stock linuxserver image ignores those fields, so the s6 run script Solaris
already mounts (#1142) now launches a small wrapper module instead of
`python3 -m wyoming_faster_whisper`. The wrapper runs the stock server with one
patch — which means it lives or dies with upstream's shape, so it asserts that
shape at startup and, when it moved, starts the stock server WITHOUT the patch
(#1193: the hints are an enhancement, STT is the service). These tests exercise
the wrapper against a stub of that shape (real files on disk: the assertion
reads `inspect.getsource`)."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import re
import subprocess
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]

# What the mounted run script actually execs, values and all.
ARGV = [
    "--uri",
    "tcp://127.0.0.1:10300",
    "--model",
    "medium-int8",
    "--device",
    "cuda",
    "--beam-size",
    "1",
    "--language",
    "de",
    "--vad-filter",
    "--data-dir",
    "/config",
    "--download-dir",
    "/config",
    "--initial-prompt",
    "Geräte: Leselicht.",
]

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


def _dispatch_handler_src(prompt_hook=True, sites=2):
    """The v3.6.0-ls59 handler, reduced to the two prompt sites the patch needs.

    `prompt_hook=False` is the pre-3.6.0 shape (the prompt read straight off the
    loader), i.e. an image whose interception point the wrapper cannot find."""
    head = (
        "SEEN = []\n\n\n"
        "class DispatchEventHandler:\n"
        "    def __init__(self, loader):\n"
        "        self._loader = loader\n\n"
    )
    if not prompt_hook:
        return head + (
            "    async def handle_event(self, event):\n"
            "        SEEN.append(dict(initial_prompt=self._loader.initial_prompt))\n"
            "        return True\n"
        )
    commit = (
        "\n    async def _commit_path(self):\n"
        "        return dict(initial_prompt=await self._initial_prompt(wait=False))\n"
        if sites >= 2
        else ""
    )
    return head + (
        "    async def handle_event(self, event):\n"
        "        SEEN.append(dict(initial_prompt=await self._initial_prompt()))\n"
        "        return True\n"
        f"{commit}"
        "\n    async def _initial_prompt(self, wait=True):\n"
        "        return self._loader.initial_prompt\n"
    )


def _run_script_flags(pd):
    """The flags the mounted s6 run script hands the wrapper."""
    return sorted(set(re.findall(r"--[a-z][a-z-]*", pd.WHISPER_RUN_SCRIPT)))


def _stub_tree(root, hint_fields=True, flags=None, **handler):
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
        '__version__ = "3.6.0"\n'
    )
    # The wrapper reads the installed package's own source to learn which CLI
    # flags it still accepts, so the stub has to name them the way upstream does.
    cli = "".join(f'    parser.add_argument("{flag}")\n' for flag in flags or ())
    (root / "wyoming_faster_whisper" / "__main__.py").write_text(
        "import argparse\nimport sys\n\nRAN = []\nARGV = []\n\n\n"
        f"def _parser():\n    parser = argparse.ArgumentParser()\n{cli}"
        "    return parser\n\n\n"
        "def run():\n    RAN.append(True)\n    ARGV.append(list(sys.argv[1:]))\n"
    )
    (root / "wyoming_faster_whisper" / "dispatch_handler.py").write_text(
        _dispatch_handler_src(**handler)
    )


class _Loader:
    """Stands in for the server's shared ModelLoader."""

    def __init__(self, initial_prompt):
        self.initial_prompt = initial_prompt
        self.beam_size = 1


def _wrapper(pd, tmp_path, monkeypatch, argv=None, **stub):
    stub.setdefault("flags", _run_script_flags(pd))
    _stub_tree(tmp_path, **stub)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["/solaris_whisper_hints.py", *(argv or ARGV)])
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


def _prompt_seen():
    from wyoming_faster_whisper.dispatch_handler import SEEN

    return SEEN[-1]["initial_prompt"]


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
    assert _prompt_seen() == (
        "Sprachbefehle im Haushalt. Die Geräte heißen: Leselicht. Thorgrim, Silbermark."
    )
    # The shared loader itself is never rewritten.
    assert loader.initial_prompt.endswith("Leselicht.")


def test_the_streaming_path_gets_the_same_words(pd, tmp_path, monkeypatch):
    # 3.6.0 asks for the prompt twice: at AudioStop for the batch transcriber and
    # at _commit_path for a streaming session. Both must carry the words.
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    handler = DispatchEventHandler(_Loader("Geräte: Leselicht."))
    asyncio.run(handler.handle_event(_transcribe_event(transcript_terms=["Thorgrim"])))
    committed = asyncio.run(handler._commit_path())
    assert committed["initial_prompt"] == "Geräte: Leselicht. Thorgrim."


def test_a_request_without_words_is_untouched(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    loader = _Loader("Die Geräte heißen: Leselicht.")
    handler = DispatchEventHandler(loader)
    asyncio.run(handler.handle_event(_transcribe_event(language="de")))
    # Not merely equal — the same object: today's behaviour, byte for byte.
    assert _prompt_seen() is loader.initial_prompt


def test_a_second_request_does_not_stack_onto_the_first(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    handler = DispatchEventHandler(_Loader("Geräte: Leselicht."))
    for name in ("Thorgrim", "Yseult"):
        asyncio.run(handler.handle_event(_transcribe_event(transcript_names=[name])))
    assert _prompt_seen() == "Geräte: Leselicht. Yseult."


def test_startup_names_the_version_it_matched(pd, tmp_path, monkeypatch, capsys):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    line = capsys.readouterr().err
    assert "solaris-hints: active on wyoming-faster-whisper 3.6.0" in line
    from wyoming_faster_whisper.__main__ import RAN

    assert RAN == [True]


@pytest.mark.parametrize(
    "drift, marker",
    [
        ({"prompt_hook": False}, "no _initial_prompt"),
        ({"sites": 1}, "expected 2"),
        ({"hint_fields": False}, "transcript_names"),
    ],
    ids=["hook-gone", "one-prompt-site", "fields-gone"],
)
def test_an_unknown_upstream_shape_still_starts_the_server(
    pd, tmp_path, monkeypatch, capsys, drift, marker
):
    # #1193: an unattended linuxserver bump (AutoUpdate=registry) restructured
    # the module this patch reaches into, and the old fail-closed guard took the
    # whole household's speech-to-text down with it. The hints are an
    # enhancement; the server must come up regardless, and say so loudly.
    mod = _wrapper(pd, tmp_path, monkeypatch, **drift)
    assert mod.main() == 0
    err = capsys.readouterr().err
    assert "solaris-hints: NOT APPLIED" in err
    assert marker in err
    assert "STT works" in err
    from wyoming_faster_whisper.__main__ import RAN

    assert RAN == [True]


# -- upstream CLI flag drift (#1210) ------------------------------------------


def _served_argv():
    from wyoming_faster_whisper.__main__ import ARGV as SERVED

    return SERVED[-1]


def test_the_run_scripts_flags_reach_the_server_untouched(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch)
    assert mod.main() == 0
    assert _served_argv() == ARGV


def test_a_flag_upstream_dropped_does_not_take_stt_down(
    pd, tmp_path, monkeypatch, capsys
):
    # #1210: the mounted run script spells upstream's flags out verbatim and the
    # image carries AutoUpdate=registry, so a flag that goes away arrives
    # unannounced — and argparse would exit 2 before the server ever binds.
    known = [f for f in _run_script_flags(pd) if f != "--vad-filter"]
    mod = _wrapper(pd, tmp_path, monkeypatch, flags=known)
    assert mod.main() == 0
    served = _served_argv()
    assert "--vad-filter" not in served
    assert served == [a for a in ARGV if a != "--vad-filter"]
    err = capsys.readouterr().err
    assert "solaris-whisper-flags: --vad-filter NOT accepted" in err
    assert "STT still starts" in err
    from wyoming_faster_whisper.__main__ import RAN

    assert RAN == [True]


def test_a_dropped_flag_takes_its_value_with_it(pd, tmp_path, monkeypatch):
    # A value left behind would be read as a positional and fail just as hard.
    known = [f for f in _run_script_flags(pd) if f != "--initial-prompt"]
    mod = _wrapper(pd, tmp_path, monkeypatch, flags=known)
    assert mod.main() == 0
    assert _served_argv() == ARGV[: ARGV.index("--initial-prompt")]


def test_an_unreadable_server_package_changes_nothing(pd, tmp_path, monkeypatch):
    # No source to check against is not evidence a flag is gone — hand the
    # invocation over as-is rather than guess it apart.
    mod = _wrapper(pd, tmp_path, monkeypatch, flags=[])
    monkeypatch.setattr(mod, "cli_source", lambda: "")
    assert mod.main() == 0
    assert _served_argv() == ARGV


def test_a_degraded_start_leaves_the_prompt_alone(pd, tmp_path, monkeypatch):
    mod = _wrapper(pd, tmp_path, monkeypatch, prompt_hook=False)
    assert mod.main() == 0
    from wyoming_faster_whisper.dispatch_handler import DispatchEventHandler

    loader = _Loader("Geräte: Leselicht.")
    handler = DispatchEventHandler(loader)
    asyncio.run(handler.handle_event(_transcribe_event(transcript_names=["Thorgrim"])))
    assert _prompt_seen() == "Geräte: Leselicht."
