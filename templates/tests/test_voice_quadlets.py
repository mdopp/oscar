"""Tests for the Solaris-owned voice-pipeline Quadlet rendering (#456).

Solaris owns its whole voice pipeline. The GPU services — whisper STT, the
Kokoro-Martin TTS and the wakeword trainer (#1066) — run as companion
`.container` Quadlets the post-deploy writes (CDI is dropped in kube-play pods,
#1026); the CPU services —
openWakeWord and the wyoming TTS bridge — ride the solaris pod (template.yml).
The render_* functions are pure, so they're unit-tested directly (mirroring the
ServiceBay voice template's own quadlet-render tests)."""

from __future__ import annotations

import importlib.util
import pathlib
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
        lambda data_dir, model, language, gpu: (
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
        lambda data_dir, model, language, gpu: (
            rendered.update(model=model, gpu=gpu) or "UNIT"
        ),
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    assert rendered == {"model": "small-int8", "gpu": False}
    assert (tmp_path / "voice" / "whisper").is_dir()


def test_stt_healthcheck_probe_is_valid_python(pd):
    # The probe is shipped as a string and executed inside the whisper
    # container; a syntax error would silently disable the self-heal (#610).
    compile(pd.STT_HEALTHCHECK, "stt_healthcheck.py", "exec")
    assert "transcript" in pd.STT_HEALTHCHECK
    assert "10300" in pd.STT_HEALTHCHECK


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


# The line above is necessary but NOT sufficient, which is exactly how #1090
# shipped green and #1092 came back: `Pull=newer` decides what a start pulls, it
# never causes a start. The tests below cover the half that actually makes a
# deploy effective — the conditional restart.


class _FakePodman:
    """Stand-in for subprocess.run covering the podman/systemctl calls the
    trainer refresh makes. `running`/`latest` are image ids."""

    def __init__(self, running: str, latest: str, in_flight: str = "0"):
        self.running, self.latest, self.in_flight = running, latest, in_flight
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        out, rc = "", 0
        if args[:2] == ["podman", "exec"]:
            out, rc = (self.in_flight, 0) if self.in_flight is not None else ("", 1)
        elif args[:2] == ["podman", "inspect"]:
            out = self.running
        elif args[:3] == ["podman", "image", "inspect"]:
            out = self.latest
        return type("_P", (), {"returncode": rc, "stdout": out, "stderr": ""})()

    @property
    def restarted(self) -> bool:
        return any(c[:3] == ["systemctl", "--user", "restart"] for c in self.calls)

    @property
    def pulled(self) -> bool:
        return any(c[:2] == ["podman", "pull"] for c in self.calls)


def test_wakeword_trainer_queue_probe_is_valid_python(pd):
    # Shipped as a `python3 -c` string into the chat container; a syntax error
    # would read as "queue unknown" forever and silently disable the refresh.
    compile(pd.WAKEWORD_QUEUE_PROBE, "queue_probe.py", "exec")
    assert "wakeword_training_runs" in pd.WAKEWORD_QUEUE_PROBE
    assert "mode=ro" in pd.WAKEWORD_QUEUE_PROBE


def test_idle_trainer_is_restarted_onto_a_rebuilt_image(pd, monkeypatch):
    """The #1092 case: unit file unchanged, registry moved, nothing training."""
    fake = _FakePodman(running="sha256:aaaa111122223333", latest="bbbb444455556666")
    monkeypatch.setattr(pd, "service_active", lambda unit: True)
    monkeypatch.setattr(pd.subprocess, "run", fake)
    assert pd.refresh_wakeword_trainer_image() is True
    assert fake.pulled and fake.restarted


def test_trainer_is_not_restarted_while_a_run_is_in_flight(pd, monkeypatch):
    # A run takes hours; restarting would throw it away (trainer.py then marks
    # the orphaned row failed — honest, but the hours are gone).
    fake = _FakePodman(running="aaaa", latest="bbbb", in_flight="1")
    monkeypatch.setattr(pd, "service_active", lambda unit: True)
    monkeypatch.setattr(pd.subprocess, "run", fake)
    assert pd.refresh_wakeword_trainer_image() is False
    assert not fake.restarted
    # Not even the pull — a multi-GB download mid-run is disk churn for nothing.
    assert not fake.pulled


def test_trainer_is_not_restarted_when_the_queue_cannot_be_read(pd, monkeypatch):
    # Unknown is not idle: a run we cannot see is a run we must not abort.
    fake = _FakePodman(running="aaaa", latest="bbbb", in_flight=None)
    monkeypatch.setattr(pd, "service_active", lambda unit: True)
    monkeypatch.setattr(pd.subprocess, "run", fake)
    assert pd.refresh_wakeword_trainer_image() is False
    assert not fake.restarted


def test_trainer_is_not_restarted_when_the_image_is_current(pd, monkeypatch):
    # The common deploy: idle, but nothing new to run. No pointless churn.
    fake = _FakePodman(running="sha256:aaaa1111", latest="aaaa1111")
    monkeypatch.setattr(pd, "service_active", lambda unit: True)
    monkeypatch.setattr(pd.subprocess, "run", fake)
    assert pd.refresh_wakeword_trainer_image() is False
    assert not fake.restarted


def test_trainer_refresh_skips_an_inactive_unit(pd, monkeypatch):
    # install_unit already (re)starts an inactive unit — Pull=newer covers it.
    monkeypatch.setattr(pd, "service_active", lambda unit: False)
    monkeypatch.setattr(
        pd.subprocess, "run", lambda *a, **k: pytest.fail("must not probe or pull")
    )
    assert pd.refresh_wakeword_trainer_image() is False


def test_install_wakeword_trainer_unit_refreshes_a_stale_image(
    pd, monkeypatch, tmp_path
):
    # The wiring: a no-op install must still reach the staleness check, or the
    # whole fix never runs on the deploy that needs it.
    called = []
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    monkeypatch.setattr(
        pd, "refresh_wakeword_trainer_image", lambda: called.append(True) or False
    )
    assert pd.install_wakeword_trainer_unit(str(tmp_path)) is True
    assert called == [True]


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
    monkeypatch.setattr(pd, "refresh_wakeword_trainer_image", lambda: False)
    assert pd.install_wakeword_trainer_unit(str(tmp_path)) is True
    assert written == [pd.WAKEWORD_TRAINER_UNIT]
    # Quadlet Volume= does not create the host dir (unlike kube DirectoryOrCreate).
    assert (tmp_path / "solaris" / "wakeword-train").is_dir()


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
