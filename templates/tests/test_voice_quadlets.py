"""Tests for the Solaris-owned voice-pipeline Quadlet rendering (#456).

Solaris owns its whole voice pipeline. The GPU services — whisper STT, the
Kokoro-Martin TTS and the wakeword trainer (#1066) — run as companion
`.container` Quadlets the post-deploy writes (CDI is dropped in kube-play pods,
#1026); the CPU services —
openWakeWord and the wyoming TTS bridge — ride the solaris pod (template.yml).
The render_* functions are pure, so they're unit-tested directly (mirroring the
ServiceBay voice template's own quadlet-render tests)."""

from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import symtable
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solaris_pd_quadlets", TEMPLATES / "solaris" / "post-deploy.py")


# -- whisper -----------------------------------------------------------------


def test_whisper_gpu_unit_has_cdi_device_and_selinux_relax(pd):
    unit = pd.render_whisper_unit("/mnt/data", "medium-int8", "de", gpu=True)
    # #1026: CDI device must be AddDevice= on the quadlet, never resources.limits.
    assert "AddDevice=nvidia.com/gpu=all" in unit
    assert "SecurityLabelDisable=true" in unit
    assert "Image=lscr.io/linuxserver/faster-whisper:gpu" in unit
    assert "Environment=WHISPER_MODEL=medium-int8" in unit
    assert "Environment=WHISPER_LANG=de" in unit
    assert "Network=host" in unit
    # GPU image keeps its model cache under /config.
    assert "Volume=/mnt/data/voice/whisper-gpu:/config:Z" in unit
    # STT health probe + self-heal (#610): a wedged CUDA context keeps the
    # container Up while every transcription fails, so the only way it heals is
    # a healthcheck that exercises STT and kills the container on failure.
    assert "Volume=/mnt/data/voice/stt_healthcheck.py:/stt_healthcheck.py:ro,Z" in unit
    assert "HealthCmd=python3 /stt_healthcheck.py" in unit
    assert "HealthOnFailure=kill" in unit
    assert "HealthRetries=3" in unit


def test_whisper_cpu_unit_uses_cpu_image_and_wyoming_port(pd):
    unit = pd.render_whisper_unit("/mnt/data", "base-int8", "de", gpu=False)
    assert "AddDevice" not in unit
    assert "SecurityLabelDisable" not in unit
    assert "Image=docker.io/rhasspy/wyoming-whisper:latest" in unit
    # Same Wyoming endpoint as GPU (the linuxserver image binds :10300 itself).
    assert "--uri tcp://0.0.0.0:10300" in unit
    assert "--model base-int8 --language de" in unit
    assert "Volume=/mnt/data/voice/whisper:/data:Z" in unit
    # The STT self-heal probe applies to the CPU path too (#610).
    assert "Volume=/mnt/data/voice/stt_healthcheck.py:/stt_healthcheck.py:ro,Z" in unit
    assert "HealthCmd=python3 /stt_healthcheck.py" in unit
    assert "HealthOnFailure=kill" in unit


def test_install_whisper_unit_picks_gpu_model_default_on_cdi(pd, monkeypatch, tmp_path):
    rendered = {}
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd,
        "render_whisper_unit",
        lambda data_dir, model, language, gpu, prompt="": (
            rendered.update(model=model, gpu=gpu) or "UNIT"
        ),
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    # base-int8 default + GPU box ⇒ auto-upgrade to medium-int8.
    assert rendered == {"model": "medium-int8", "gpu": True}
    assert (tmp_path / "voice" / "whisper-gpu").is_dir()
    # The STT health probe is dropped next to the cache dir (#610).
    probe = tmp_path / "voice" / "stt_healthcheck.py"
    assert probe.is_file()
    assert "transcript" in probe.read_text()


def test_install_whisper_unit_keeps_explicit_model_on_cpu(pd, monkeypatch, tmp_path):
    rendered = {}
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(
        pd,
        "env",
        lambda key, default="": "small-int8" if key == "WHISPER_MODEL" else default,
    )
    monkeypatch.setattr(
        pd,
        "render_whisper_unit",
        lambda data_dir, model, language, gpu, prompt="": (
            rendered.update(model=model, gpu=gpu) or "UNIT"
        ),
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    assert rendered == {"model": "small-int8", "gpu": False}
    assert (tmp_path / "voice" / "whisper").is_dir()


# -- whisper initial prompt (#1142) ------------------------------------------
#
# Device, room and scene names are self-chosen proper nouns, so whisper guesses
# them by sound ("Lichterkette Balkon" → anything). faster-whisper takes an
# `initial_prompt`, but the linuxserver GPU image's s6 run script hardcodes its
# flags and wires no env var to it — hence the run-script override below.


def test_whisper_gpu_unit_mounts_the_run_script_and_passes_the_prompt(pd):
    unit = pd.render_whisper_unit(
        "/mnt/data", "medium-int8", "de", gpu=True, prompt="Namen: Leselicht."
    )
    assert (
        "Volume=/mnt/data/voice/whisper-run:"
        "/etc/s6-overlay/s6-rc.d/svc-whisper/run:ro,Z" in unit
    )
    # Quoted: the value carries spaces, and systemd would otherwise read only
    # the first word as the variable and the rest as further assignments.
    assert 'Environment="WHISPER_PROMPT=Namen: Leselicht."' in unit


def test_whisper_unit_without_a_prompt_declares_no_prompt_env(pd):
    # First deploy of a box: no HA names yet ⇒ no empty prompt handed to whisper.
    unit = pd.render_whisper_unit("/mnt/data", "medium-int8", "de", gpu=True)
    assert "WHISPER_PROMPT" not in unit
    cpu = pd.render_whisper_unit("/mnt/data", "base-int8", "de", gpu=False)
    assert "--initial-prompt" not in cpu


def test_whisper_cpu_unit_takes_the_prompt_as_a_cli_flag(pd):
    # The rhasspy image runs the server directly, so no run-script override.
    cpu = pd.render_whisper_unit(
        "/mnt/data", "base-int8", "de", gpu=False, prompt="Namen: Leselicht."
    )
    assert '--initial-prompt "Namen: Leselicht."' in cpu
    assert "svc-whisper" not in cpu


def test_whisper_run_script_keeps_upstreams_flags_and_adds_the_prompt(pd):
    # It REPLACES the image's own run script, so dropping one of upstream's
    # flags would silently change how whisper runs (wrong model, wrong cache).
    script = pd.WHISPER_RUN_SCRIPT
    for flag in ("--uri", "--model", "--beam-size", "--language", "--data-dir"):
        assert flag in script
    # Since #1157 the script launches the hint wrapper, which runs the stock
    # server itself — see test_whisper_hints.py.
    assert "/solaris_whisper_hints.py" in script
    # Quoted array expansion: an unquoted "$WHISPER_PROMPT" would word-split the
    # names into separate argv entries and the server would reject them.
    assert 'prompt_args=(--initial-prompt "${WHISPER_PROMPT}")' in script
    assert '"${prompt_args[@]}"' in script


# -- non-speech must not become a device name (#1158) ------------------------
#
# The prompt above cuts both ways: priming the decoder on the household's proper
# nouns also teaches it to emit them when it hears nothing, so a wake-word false
# trigger recording room noise could be transcribed into a device name and become
# a command nobody spoke. Box-measured on the deployed GPU build: 7 of 10
# non-speech inputs decoded to device names without `--vad-filter`, 0 with it,
# and no real utterance was lost.


def test_whisper_run_script_filters_non_speech(pd):
    script = pd.WHISPER_RUN_SCRIPT
    assert "--vad-filter" in script
    # In the server's argv, not the CUDA preamble — a flag that never reaches
    # the wrapper is a fix that silently does nothing.
    assert "--vad-filter" in script.split("\nexec \\")[1]


def test_whisper_run_script_leaves_the_vad_thresholds_at_upstream_defaults(pd):
    # The opposite failure is worse than the one being fixed: a tighter VAD
    # discards a quiet, sleepy "mach das Licht aus" — the household's normal
    # use. Upstream's defaults were measured to keep those; don't tune them
    # without measuring the quiet direction again.
    script = pd.WHISPER_RUN_SCRIPT
    for flag in ("--vad-threshold", "--vad-min-speech-ms", "--vad-min-silence-ms"):
        assert flag not in script


def test_whisper_run_script_still_primes_the_device_names(pd):
    # Filtering non-speech is not a licence to drop #1142/#1157: the names still
    # have to reach the decoder for real commands.
    script = pd.WHISPER_RUN_SCRIPT
    assert '"${prompt_args[@]}"' in script
    assert "/solaris_whisper_hints.py" in script


# -- whisper on the GPU (#1162) ----------------------------------------------
#
# The card was passed through but never used: wyoming-faster-whisper defaults
# --device to cpu, and the :gpu image puts its own pip cuBLAS/cuDNN on no
# library path — so `cuda` would load the model into VRAM and then die on the
# first encode. Both have to hold, or the household's only voice path breaks.


def _run_script_preamble(pd) -> str:
    """Everything the run script does before exec'ing the server, as a
    standalone bash program that reports the library path it derived."""
    head = pd.WHISPER_RUN_SCRIPT.split("\nexec \\")[0]
    return head + '\necho "$LD_LIBRARY_PATH"\n'


def test_whisper_run_script_asks_for_the_gpu(pd):
    assert "--device cuda" in pd.WHISPER_RUN_SCRIPT


def test_whisper_cpu_unit_never_asks_for_a_device(pd):
    # The rhasspy CPU image has no CUDA at all, and it runs the server directly
    # — no run-script wrapper to carry a device flag onto.
    cpu = pd.render_whisper_unit("/mnt/data", "base-int8", "de", gpu=False)
    assert "--device" not in cpu
    assert "LD_LIBRARY_PATH" not in cpu
    assert "whisper-run" not in cpu


def test_whisper_run_script_is_valid_bash(pd, tmp_path):
    # It replaces the image's own s6 run script; a syntax error takes STT down.
    script = tmp_path / "run"
    script.write_text(pd.WHISPER_RUN_SCRIPT)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def _fake_cuda_tree(root: pathlib.Path, *libs: str) -> pathlib.Path:
    for lib in libs:
        pkg, soname = lib.split("/")
        d = root / "nvidia" / pkg / "lib"
        d.mkdir(parents=True, exist_ok=True)
        (d / soname).touch()
    return root


def _derive(pd, tmp_path, pythonpath: str):
    script = tmp_path / "preamble.sh"
    script.write_text(_run_script_preamble(pd))
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": pythonpath},
    )


def test_whisper_run_script_derives_the_cuda_library_path(pd, tmp_path):
    # Derived from the interpreter, never a hardcoded site-packages path: the
    # image's python version moves and a stale literal would silently be wrong.
    root = _fake_cuda_tree(
        tmp_path / "site", "cublas/libcublas.so.12", "cudnn/libcudnn.so.9"
    )
    out = _derive(pd, tmp_path, str(root))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == (f"{root}/nvidia/cublas/lib:{root}/nvidia/cudnn/lib")


def test_whisper_run_script_refuses_to_start_without_the_cuda_libraries(pd, tmp_path):
    # A silent fall back to the CPU is the exact bug #1162 fixes — a missing
    # library must take the service down where it can be seen.
    root = _fake_cuda_tree(tmp_path / "site", "cublas/libcublas.so.12")
    out = _derive(pd, tmp_path, str(root))
    assert out.returncode != 0
    assert "libcudnn.so.9" in out.stderr


def test_whisper_prompt_stays_inside_whispers_224_token_window(pd):
    # Whisper keeps only the LAST 224 tokens: an over-long prompt drops its own
    # lead-in, and every surplus rare word is one more hallucination candidate.
    names = [f"Lichterkette Balkon Nummer {i}" for i in range(200)]
    prompt = pd.build_whisper_prompt(names, "de")
    assert len(prompt) <= pd.WHISPER_PROMPT_MAX_CHARS
    assert prompt.startswith(pd.WHISPER_PROMPT_LEAD["de"])
    assert prompt.endswith(".")
    # Truncation drops names off the END, it never mangles one.
    assert "Nummer 0," in prompt


def test_whisper_prompt_is_empty_without_names(pd):
    assert pd.build_whisper_prompt([], "de") == ""


def test_whisper_prompt_falls_back_to_english_for_other_languages(pd):
    assert pd.build_whisper_prompt(["Reading Light"], "fr").startswith(
        pd.WHISPER_PROMPT_LEAD["en"]
    )


def _states(*entities):
    return [
        {"entity_id": eid, "attributes": {"friendly_name": name}}
        for eid, name in entities
    ]


def test_whisper_prompt_names_only_takes_spoken_about_domains(pd, monkeypatch):
    monkeypatch.setattr(
        pd,
        "_ha_get",
        lambda path, token: (
            200,
            _states(
                ("light.a", "Leselicht"),
                ("scene.b", "Guten Morgen"),
                # Machine-generated names: hundreds of them would eat the whole
                # budget and prime whisper on words nobody says.
                ("sensor.c", "Xiaomi Temperatur Batterie"),
                ("automation.d", "Rollos runter um 22 Uhr"),
            ),
        ),
    )
    assert pd.whisper_prompt_names("t") == ["Leselicht", "Guten Morgen"]


def test_whisper_prompt_names_dedupe_and_strip_quoting_hazards(pd, monkeypatch):
    # The names land inside a systemd `Environment="…"` value: a stray quote or
    # a `%` specifier there breaks the unit, not just the prompt.
    monkeypatch.setattr(
        pd,
        "_ha_get",
        lambda path, token: (
            200,
            _states(
                ("light.a", 'Bad "100%" Licht'),
                ("switch.b", "Leselicht"),
                ("light.c", "Leselicht"),
                ("light.d", ""),
            ),
        ),
    )
    assert pd.whisper_prompt_names("t") == ["Bad 100 Licht", "Leselicht"]


def test_converge_whisper_prompt_reinstalls_only_on_change(pd, monkeypatch, tmp_path):
    (tmp_path / "voice").mkdir()
    installs = []
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(pd, "whisper_prompt_names", lambda token: ["Leselicht"])
    monkeypatch.setattr(pd, "install_whisper_unit", lambda d: installs.append(d))
    prompt = pd.converge_whisper_prompt("t", str(tmp_path))
    assert "Leselicht" in prompt
    assert installs == [str(tmp_path)]
    # Persisted, so the next deploy starts whisper primed before HA answers.
    assert pd.read_whisper_prompt(str(tmp_path)) == prompt
    # Same household names on the next deploy ⇒ no STT restart.
    assert pd.converge_whisper_prompt("t", str(tmp_path)) == prompt
    assert installs == [str(tmp_path)]


def test_converge_whisper_prompt_keeps_the_old_prompt_when_ha_is_unreadable(
    pd, monkeypatch, tmp_path
):
    (tmp_path / "voice").mkdir()
    (tmp_path / "voice" / pd.WHISPER_PROMPT_FILE).write_text("Namen: Leselicht.\n")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(pd, "whisper_prompt_names", lambda token: [])
    monkeypatch.setattr(
        pd,
        "install_whisper_unit",
        lambda d: pytest.fail("an unreadable HA must not restart STT"),
    )
    assert pd.converge_whisper_prompt("t", str(tmp_path)) == "Namen: Leselicht."


def test_install_whisper_unit_drops_the_run_script_next_to_the_cache(
    pd, monkeypatch, tmp_path
):
    """Without the file on disk podman bind-mounts a fresh empty DIRECTORY over
    the image's s6 run script and whisper never starts — so the unit must not be
    installed when the script could not be written."""
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    run = tmp_path / "voice" / "whisper-run"
    assert run.read_text() == pd.WHISPER_RUN_SCRIPT
    assert run.stat().st_mode & 0o111, "s6 will not exec a non-executable run script"


def test_install_whisper_unit_passes_the_persisted_prompt(pd, monkeypatch, tmp_path):
    (tmp_path / "voice").mkdir()
    (tmp_path / "voice" / pd.WHISPER_PROMPT_FILE).write_text("Namen: Leselicht.\n")
    seen = {}
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd,
        "render_whisper_unit",
        lambda data_dir, model, language, gpu, prompt="": (
            seen.update(prompt=prompt) or "UNIT"
        ),
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    assert seen == {"prompt": "Namen: Leselicht."}


def test_stt_healthcheck_probe_is_valid_python(pd):
    # The probe is shipped as a string and executed inside the whisper
    # container; a syntax error would silently disable the self-heal (#610).
    compile(pd.STT_HEALTHCHECK, "stt_healthcheck.py", "exec")
    assert "transcript" in pd.STT_HEALTHCHECK
    assert "10300" in pd.STT_HEALTHCHECK


def _undefined_globals(source: str) -> set[str]:
    """Names the script reads but never defines — i.e. everything it would have
    to borrow from the namespace of whatever generated it."""
    top = symtable.symtable(source, "embedded.py", "exec")
    defined = set(top.get_identifiers()) | set(dir(builtins))
    leaked: set[str] = set()
    tables = list(top.get_children())
    while tables:
        table = tables.pop()
        tables.extend(table.get_children())
        leaked |= {
            sym.get_name()
            for sym in table.get_symbols()
            if sym.is_global() and sym.get_name() not in defined
        }
    return leaked


@pytest.mark.parametrize(
    "attr", ["WAKEWORD_QUEUE_PROBE", "STT_HEALTHCHECK"], ids=lambda a: a.lower()
)
def test_embedded_probes_borrow_nothing_from_post_deploy_s_namespace(pd, attr):
    """#1144: the STT probe called post-deploy's own `system_language()`.

    It compiled, so the sibling syntax check passed — but it runs in the whisper
    container as its own process, where that name does not exist, and the
    HealthCmd had been dying with NameError since #611 while the box reported no
    STT health signal at all. Every embedded script must stand alone."""
    assert not _undefined_globals(getattr(pd, attr))


def test_the_written_stt_probe_has_no_unfilled_placeholder(pd):
    # A shipped `__LANGUAGE__` would ask whisper to transcribe in a language
    # that does not exist — the failure the raw constant invites.
    rendered = pd.render_stt_healthcheck("nl")
    assert '"language": LANGUAGE' in rendered
    assert 'LANGUAGE = "nl"' in rendered
    assert not re.search(r"__[A-Z]+__", rendered)


def test_the_probe_asks_for_the_language_whisper_actually_runs(
    pd, monkeypatch, tmp_path
):
    # WHISPER_LANGUAGE overrides system_language() for the container, so the
    # probe has to follow it or it transcribes against a different model config.
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(
        pd,
        "env",
        lambda key, default="": "nl" if key == "WHISPER_LANGUAGE" else default,
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    probe = (tmp_path / "voice" / "stt_healthcheck.py").read_text()
    assert 'LANGUAGE = "nl"' in probe
    assert not _undefined_globals(probe)


# -- Kokoro-Martin TTS + bridge ----------------------------------------------


def test_tts_unit_is_solaris_image_martin_voice_with_cdi(pd):
    unit = pd.render_tts_unit()
    # The RENAMED bundled image, not solilos-tts.
    assert "Image=ghcr.io/mdopp/solaris-tts:latest" in unit
    assert "Environment=KOKORO_ONNX_VOICE=martin" in unit
    assert "Environment=KOKORO_ONNX_LANG=de" in unit
    assert "Environment=KOKORO_ONNX_PROVIDER=cuda" in unit
    assert "AddDevice=nvidia.com/gpu=all" in unit
    assert "SecurityLabelDisable=true" in unit


# The TTS bridge and openWakeWord are CPU containers in the solaris pod
# (template.yml), not Quadlets — so there are no render_*/install_* funcs to
# unit-test here; their pod-spec presence is asserted in test_engine_topology.


def test_install_tts_units_skips_without_cdi(pd, monkeypatch):
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(
        pd,
        "install_unit",
        lambda *a: pytest.fail("must not write TTS units on CPU box"),
    )
    assert pd.install_tts_units() is False


def test_install_tts_units_writes_only_kokoro_on_gpu(pd, monkeypatch):
    written = []
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(
        pd, "install_unit", lambda unit, content: written.append(unit) or True
    )
    assert pd.install_tts_units() is True
    # The bridge moved into the pod — only the GPU Kokoro TTS Quadlet is written.
    assert written == [pd.TTS_UNIT]


# -- wakeword trainer --------------------------------------------------------


def test_wakeword_trainer_unit_mounts_the_queue_db_and_work_dir(pd):
    unit = pd.render_wakeword_trainer_unit("/mnt/data")
    assert "Image=ghcr.io/mdopp/solaris-wakeword-trainer:latest" in unit
    # #1026: CDI device must be AddDevice= on the quadlet, never resources.limits.
    assert "AddDevice=nvidia.com/gpu=all" in unit
    assert "SecurityLabelDisable=true" in unit
    # The queue lives in the pod's solaris.db — without this mount the trainer
    # polls an empty path forever and every enqueued run just sits there.
    assert "Volume=/mnt/data/solarisbay:/var/lib/solaris:Z" in unit
    assert "Volume=/mnt/data/solaris/wakeword-train:/work:Z" in unit
    assert "ShmSize=8g" in unit


def test_wakeword_trainer_unit_allows_a_long_first_pull(pd):
    """The TF-GPU base is several GB and podman derives its pull timeout from
    TimeoutStartSec. Box-observed: systemd killed the unit 4m15s into the first
    pull, and each retry restarted the download from the top — a crash loop that
    could never converge."""
    unit = pd.render_wakeword_trainer_unit("/mnt/data")
    timeout = next(
        (
            int(ln.split("=", 1)[1])
            for ln in unit.splitlines()
            if ln.startswith("TimeoutStartSec=")
        ),
        0,
    )
    assert timeout >= 1800, "a multi-GB first pull needs more than the systemd default"


def test_wakeword_trainer_unit_picks_up_a_rebuilt_image(pd):
    """A deploy must not leave the trainer on the image it started with.

    Box-observed 2026-07-28: after #1074 taught the trainer to fold residents'
    own recordings into the positives, a deploy logged "current and active —
    no-op" and the unit kept running the previous build. The chat surface was
    already offering the new training button, so a run would have reported
    success while quietly ignoring every recording the resident made.
    """
    unit = pd.render_wakeword_trainer_unit("/mnt/data")
    assert "Pull=newer\n" in unit


def test_install_wakeword_trainer_unit_skips_without_cdi(pd, monkeypatch):
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(
        pd,
        "install_unit",
        lambda *a: pytest.fail("must not write the trainer unit on CPU box"),
    )
    assert pd.install_wakeword_trainer_unit("/mnt/data") is False


def test_install_wakeword_trainer_unit_honours_the_off_switch(pd, monkeypatch):
    # A ~10 GB TensorFlow-GPU image plus its corpora is not something a
    # space-constrained box should get handed unconditionally.
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(
        pd,
        "env",
        lambda key, default="": (
            "false" if key == "WAKEWORD_TRAINER_ENABLED" else default
        ),
    )
    monkeypatch.setattr(
        pd, "install_unit", lambda *a: pytest.fail("must not write a disabled unit")
    )
    assert pd.install_wakeword_trainer_unit("/mnt/data") is False


def test_install_wakeword_trainer_unit_creates_work_dir_on_gpu(
    pd, monkeypatch, tmp_path
):
    written = []
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd, "install_unit", lambda unit, content: written.append(unit) or True
    )
    assert pd.install_wakeword_trainer_unit(str(tmp_path)) is True
    assert written == [pd.WAKEWORD_TRAINER_UNIT]
    # Quadlet Volume= does not create the host dir (unlike kube DirectoryOrCreate).
    assert (tmp_path / "solaris" / "wakeword-train").is_dir()


# -- trainer image refresh (#1092) -------------------------------------------
#
# `Pull=newer` above is necessary but NOT sufficient — it decides what a start
# pulls, never that one happens. That is how #1090 shipped green. The tests
# below cover the half that makes a deploy effective (the conditional restart)
# AND the sequencing, which is how the second attempt shipped broken: it ran the
# refresh from install_wakeword_trainer_unit, i.e. as the first action of main(),
# while ServiceBay's pod restart still had solaris-chat down — so the queue probe
# read "unknown" on 3/3 real deploys and the fail-safe declined every time.

# Every decision the refresh logs must carry the evidence a human checks at a
# glance: what the queue said, running vs available revision, did it restart.
_EVIDENCE = {"queue", "running", "available", "restarted"}


def _proc(stdout: str = "", returncode: int = 0):
    return type("_P", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


def _inspect_json(spec: str, container: bool) -> str:
    """Podman's real inspect JSON for a `<image id>|<revision>` spec: a container
    carries its image id under `.Image` with the labels in `.Config.Labels`, an
    image carries `.Id` with the labels at top level."""
    ident, _, revision = spec.partition("|")
    labels = {"org.opencontainers.image.revision": revision} if revision else {}
    if container:
        # `.Id` is the CONTAINER id here — reading it would compare the wrong
        # thing against the image inspect below.
        return json.dumps(
            [{"Id": "ccccdddd", "Image": ident, "Config": {"Labels": labels}}]
        )
    return json.dumps([{"Id": ident, "Labels": labels}])


class _FakePodman:
    """Stand-in for subprocess.run covering the podman/systemctl calls the
    trainer refresh makes. `running`/`latest` are `<image id>|<revision>`,
    rendered into the JSON podman really prints; `None` means that inspect
    fails."""

    def __init__(
        self,
        running: str | None = "sha256:aaaa11112222|8b960af",
        latest: str | None = "sha256:bbbb33334444|36c336b",
        in_flight: str | None = "0",
    ):
        self.running, self.latest, self.in_flight = running, latest, in_flight
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["podman", "image", "inspect"]:
            if self.latest is None:
                return _proc("", 1)
            return _proc(_inspect_json(self.latest, container=False))
        if args[:2] == ["podman", "exec"]:
            if self.in_flight is None:
                return _proc("", 1)
            return _proc(self.in_flight)
        if args[:2] == ["podman", "inspect"]:
            if self.running is None:
                return _proc("", 1)
            return _proc(_inspect_json(self.running, container=True))
        return _proc()

    def _did(self, prefix: list[str]) -> bool:
        return any(c[: len(prefix)] == prefix for c in self.calls)

    @property
    def restarted(self) -> bool:
        return self._did(["systemctl", "--user", "restart"])

    @property
    def pulled(self) -> bool:
        return self._did(["podman", "pull"])

    @property
    def probed(self) -> bool:
        return self._did(["podman", "exec"])

    def inspects(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[:2] == ["podman", "inspect"] or c[:3] == ["podman", "image", "inspect"]
        ]


def _refresh(pd, monkeypatch, *, active=True, chat_up=True, **fake_kwargs):
    """Run refresh_wakeword_trainer_image against a fake podman; return
    (result, fake, [(level, message, args), ...])."""
    fake = _FakePodman(**fake_kwargs)
    logs: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(pd, "service_active", lambda unit: active)
    monkeypatch.setattr(pd, "wait_for_chat", lambda *a, **k: chat_up)
    monkeypatch.setattr(pd.subprocess, "run", fake)
    monkeypatch.setattr(
        pd,
        "jlog",
        lambda level, tag, message, **args: logs.append((level, message, args)),
    )
    return pd.refresh_wakeword_trainer_image("8787"), fake, logs


def test_trainer_refresh_is_never_called_before_the_pod_is_back(pd):
    """The #1098 revert in one assertion.

    Attempt 2 called the refresh from install_wakeword_trainer_unit, which
    install_voice_pipeline runs as the very first thing main() does — before any
    of this script's readiness waits and right after ServiceBay tore the pod
    down. Only main() may call it, and not as its opening move."""
    tree = ast.parse((TEMPLATES / "solaris" / "post-deploy.py").read_text("utf-8"))
    callers, call_line, pipeline_line = set(), 0, 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id == "refresh_wakeword_trainer_image":
                callers.add(fn.name)
                call_line = node.lineno
            if fn.name == "main" and node.func.id == "install_voice_pipeline":
                pipeline_line = node.lineno
    assert callers == {"main"}, (
        "the trainer refresh reads the training queue out of solaris-chat, so it "
        f"must run from main() after the pod is back, not from {callers}"
    )
    assert call_line > pipeline_line > 0


def test_queue_probe_waits_for_chat_before_exec_ing_into_it(pd, monkeypatch):
    fake = _FakePodman()
    monkeypatch.setattr(pd.subprocess, "run", fake)
    monkeypatch.setattr(
        pd,
        "wait_for_chat",
        lambda port, timeout_secs=0: (
            bool(fake.calls.append(["wait_for_chat", port])) or True
        ),
    )
    in_flight, why = pd.wakeword_runs_in_flight("8787")
    assert in_flight == 0
    steps = [c[0] if c[0] == "wait_for_chat" else " ".join(c[:2]) for c in fake.calls]
    assert steps.index("wait_for_chat") < steps.index("podman exec")
    assert pd.CHAT_CONTAINER in why


def test_queue_probe_reports_unreadable_when_chat_never_comes_back(pd, monkeypatch):
    fake = _FakePodman()
    monkeypatch.setattr(pd.subprocess, "run", fake)
    monkeypatch.setattr(pd, "wait_for_chat", lambda *a, **k: False)
    in_flight, why = pd.wakeword_runs_in_flight("8787")
    # Not 0: an unreadable queue must never pass for an idle one.
    assert in_flight is None
    assert "/health" in why
    assert not fake.probed, "must not exec into a container that isn't up"


def test_wakeword_trainer_queue_probe_is_valid_python(pd):
    # Shipped as a `python3 -c` string into the chat container; a syntax error
    # would read as "queue unknown" forever and silently disable the refresh.
    compile(pd.WAKEWORD_QUEUE_PROBE, "queue_probe.py", "exec")
    assert "wakeword_training_runs" in pd.WAKEWORD_QUEUE_PROBE
    assert "mode=ro" in pd.WAKEWORD_QUEUE_PROBE


def test_idle_trainer_is_restarted_onto_a_rebuilt_image(pd, monkeypatch):
    """The #1092 case: unit file unchanged, registry moved, nothing training."""
    result, fake, logs = _refresh(pd, monkeypatch)
    assert result is True
    assert fake.pulled and fake.restarted
    level, message, args = logs[-1]
    assert (level, args["restarted"]) == ("info", True)
    assert (args["running"], args["available"]) == ("8b960af", "36c336b")
    assert "restarted" in message


def test_trainer_is_not_restarted_while_a_run_is_in_flight(pd, monkeypatch):
    # A run takes hours; restarting would throw it away (trainer.py then marks
    # the orphaned row failed — honest, but the hours are gone).
    result, fake, logs = _refresh(pd, monkeypatch, in_flight="1")
    assert result is False
    assert not fake.restarted
    # Not even the pull — a multi-GB download mid-run is disk churn for nothing.
    assert not fake.pulled
    level, _, args = logs[-1]
    assert level == "info"
    assert args["queue"].startswith("1 queued/running")


def test_unreadable_queue_is_logged_as_a_failure_not_a_quiet_skip(pd, monkeypatch):
    """Unknown is not idle — but it is also not "busy".

    Attempt 2 logged an unanswerable probe with the same calm info line a real
    training run gets, which is why three broken deploys looked plausible."""
    result, fake, logs = _refresh(pd, monkeypatch, in_flight=None)
    assert result is False
    assert not fake.restarted
    level, message, args = logs[-1]
    assert level == "warn", "a probe that cannot answer is a bug, not a no-op"
    assert args["queue"] == "unreadable"
    assert args["why"], "the log must say WHY the queue could not be read"
    assert "could not be read" in message


def test_unreachable_chat_is_logged_as_unreadable_not_as_busy(pd, monkeypatch):
    # The exact 3/3 box failure: chat down ⇒ no restart, but it must be loud.
    result, _, logs = _refresh(pd, monkeypatch, chat_up=False)
    assert result is False
    assert logs[-1][0] == "warn"
    assert logs[-1][2]["queue"] == "unreadable"


# The real #1092 failure mode, box-proven at last on attempt five: ServiceBay
# renders post-deploy.py through Mustache before running it, so the Go-template
# `--format` strings the identity reads used were erased at install time. podman
# was handed `--format '|'`, printed `|`, exited 0 — and both reads came back
# empty with nothing to see. Four attempts blamed timing; the script on the box
# never contained the format string at all.


def test_the_identity_read_never_asks_podman_for_a_go_template(pd, monkeypatch):
    """The regression itself. Anything in double braces is eaten by ServiceBay's
    Mustache pass before podman ever sees it, so the read must ask for JSON and
    pick the fields out here."""
    _, fake, _ = _refresh(pd, monkeypatch)
    reads = fake.inspects()
    assert reads, "the refresh must actually read both identities"
    for call in reads:
        assert not any("{{" in arg for arg in call), f"Mustache eats this: {call}"
        assert call[-2:] == ["--format", "json"]


def test_identity_reads_parse_podman_s_real_inspect_shapes(pd, monkeypatch):
    """A container reports its image under `.Image` with labels in
    `.Config.Labels`; an image reports `.Id` with labels at top level. Reading
    `.Id` off the container would compare a container id to an image id and
    restart the trainer on every deploy."""
    fake = _FakePodman()
    monkeypatch.setattr(pd.subprocess, "run", fake)
    assert pd._podman_ident(["inspect", "x"], "Image") == ("aaaa11112222", "8b960af")
    assert pd._podman_ident(["image", "inspect", "y"], "Id") == (
        "bbbb33334444",
        "36c336b",
    )


def test_an_identity_read_that_cannot_be_parsed_ends_in_no_refresh(pd, monkeypatch):
    """The fail-safe: unreadable is never treated as stale (or as current)."""
    result, fake, logs = _refresh(pd, monkeypatch, running=None, latest=None)
    assert result is False
    assert not fake.restarted
    level, message, args = logs[-1]
    assert level == "warn"
    assert (args["running"], args["available"]) == ("unreadable", "unreadable")
    assert args["queue"].startswith("idle"), "the queue read is unaffected by this"
    assert "could not read" in message


def test_an_unreadable_available_image_is_never_taken_for_a_different_one(
    pd, monkeypatch
):
    """The dangerous half of the fail-safe. Here the running container reads
    fine and only the image read stays empty, so an empty id compares *unequal*
    to a real one — dropping the guard would restart the trainer on no evidence
    at all."""
    result, fake, logs = _refresh(pd, monkeypatch, latest=None)
    assert result is False
    assert not fake.restarted
    level, _, args = logs[-1]
    assert level == "warn"
    assert (args["running"], args["available"]) == ("8b960af", "unreadable")


def test_identity_read_gives_podman_more_than_the_stock_timeout(pd, monkeypatch):
    seen: list[int | None] = []
    monkeypatch.setattr(
        pd.subprocess,
        "run",
        lambda args, **kw: (
            seen.append(kw.get("timeout"))
            or _proc(_inspect_json("sha256:aaaa|abc", container=True))
        ),
    )
    assert pd._podman_ident(["inspect", "x"], "Image")[0] == "aaaa"
    # One read, not a retry loop: the empty reads were never podman failing.
    assert seen == [pd.PODMAN_IDENT_TIMEOUT_S]
    assert pd.PODMAN_IDENT_TIMEOUT_S > 15, "podman answers while the pod restarts"


def test_trainer_is_not_restarted_when_the_image_is_current(pd, monkeypatch):
    # The common deploy: idle, but nothing new to run. No pointless churn.
    result, fake, logs = _refresh(
        pd,
        monkeypatch,
        running="sha256:aaaa11112222|8b960af",
        latest="sha256:aaaa11112222|8b960af",
    )
    assert result is False
    assert not fake.restarted
    _, _, args = logs[-1]
    assert args["running"] == args["available"] == "8b960af"


def test_trainer_refresh_skips_an_inactive_unit(pd, monkeypatch):
    result, fake, logs = _refresh(pd, monkeypatch, active=False)
    assert result is False
    assert not fake.probed and not fake.pulled and not fake.restarted
    # Nor an identity read: there is no container, and the retry budget would be
    # spent on every deploy of a box that runs no trainer.
    assert not fake.inspects()
    assert logs[-1][2]["queue"] == "not probed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"in_flight": "2"},
        {"in_flight": None},
        {"chat_up": False},
        {"active": False},
        {"latest": None},
        {"running": "sha256:aaaa11112222|8b960af", "latest": "sha256:aaaa11112222|x"},
    ],
    ids=[
        "stale",
        "busy",
        "queue-unreadable",
        "chat-down",
        "inactive",
        "image-unreadable",
        "current",
    ],
)
def test_every_refresh_outcome_logs_the_evidence(pd, monkeypatch, kwargs):
    """Whatever it decides, the deploy log must say so with the evidence.

    A line that only says "leaving the running image in place" is what let a
    non-functional refresh pass for working across three real deploys."""
    _, _, logs = _refresh(pd, monkeypatch, **kwargs)
    decisions = [entry for entry in logs if "wakeword-trainer:" in entry[1]]
    assert decisions, "the refresh must always say what it did"
    for _level, _message, args in decisions:
        assert _EVIDENCE <= set(args), f"missing evidence: {_EVIDENCE - set(args)}"


# -- openWakeWord custom-models dir ------------------------------------------


def test_setup_custom_models_dir_creates_path(pd, tmp_path):
    target = tmp_path / "voice" / "custom"
    pd.setup_custom_models_dir(str(target))
    assert target.is_dir()


def test_setup_custom_models_dir_noop_when_empty(pd, tmp_path):
    # Empty/unset ⇒ no dir created (nothing to mount).
    before = set(tmp_path.iterdir())
    pd.setup_custom_models_dir("")
    assert set(tmp_path.iterdir()) == before


# -- install_unit idempotency ------------------------------------------------


def test_install_unit_noop_when_current_and_active(pd, monkeypatch, tmp_path):
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "solaris-whisper.container").write_text("CONTENT")
    monkeypatch.setattr(
        pd.os.path,
        "expanduser",
        lambda p: (
            str(tmp_path / ".config" / "containers" / "systemd")
            if "systemd" in p
            else p
        ),
    )
    monkeypatch.setattr(pd, "service_active", lambda unit: True)
    called = []
    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: called.append(a))
    assert pd.install_unit("solaris-whisper", "CONTENT") is True
    # No daemon-reload / restart when content matches and service is active.
    assert called == []


def test_install_unit_rewrites_on_drift(pd, monkeypatch, tmp_path):
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    unit_path = systemd_dir / "solaris-whisper.container"
    unit_path.write_text("OLD")
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: str(systemd_dir) if "systemd" in p else p
    )
    monkeypatch.setattr(pd, "service_active", lambda unit: True)

    class _OK:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: _OK())
    assert pd.install_unit("solaris-whisper", "NEW") is True
    assert unit_path.read_text() == "NEW"
