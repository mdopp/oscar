"""Tests for folding the residents' own recordings into the positive set (#1074).

The wizard asks for ten "Solaris" recordings per resident and the model was
trained on none of them — it heard Piper only. These cover the dataset assembly:
that real clips reach the augmentation dir, that a clip which would poison the
positive set (silent, clipped, misencoded, recorder-rejected) does not, and that
a household with no recordings at all still trains on synthetics instead of
crashing. The training run itself is hours of GPU and is never exercised here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import struct
import sys
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load("train_micro_wake_word", ROOT / "train-micro-wake-word.py")


@pytest.fixture(scope="module")
def trainer():
    return _load("trainer", ROOT / "trainer.py")


def write_wav(
    path: pathlib.Path,
    *,
    seconds: float = 1.0,
    rate: int = 16000,
    channels: int = 1,
    width: int = 2,
    amplitude: int = 8000,
) -> pathlib.Path:
    """A `.wav` shaped like one the recorder writes: 16 kHz mono 16-bit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        step = max(1, amplitude // 4)
        pcm = b"".join(
            struct.pack("<h", amplitude if i % 8 < 4 else -amplitude + step)
            * channels
            * (width // 2)
            for i in range(frames)
        )
        wav.writeframes(pcm)
    return path


@pytest.fixture
def samples(tmp_path):
    """Two residents with the recordings the wizard collected from them."""
    root = tmp_path / "wakeword" / "user_samples"
    for index in (1, 2, 3):
        write_wav(root / "alex" / f"sample_alex_{index}.wav")
    write_wav(root / "marco" / "sample_marco_1.wav")
    return root


@pytest.fixture
def db(tmp_path):
    """solaris.db with the `wakeword_samples` table the recorder writes."""
    path = tmp_path / "solaris.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE wakeword_samples ("
            " id TEXT PRIMARY KEY, wakeword_id TEXT, filename TEXT,"
            " resident_uid TEXT, intended_phrase TEXT, stt_transcript TEXT,"
            " is_valid INTEGER NOT NULL DEFAULT 1, created_at TEXT)"
        )
        conn.commit()
    return path


def _mark(db_path: pathlib.Path, filename: pathlib.Path, *, valid: bool) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO wakeword_samples (id, filename, is_valid) VALUES (?, ?, ?)",
            (filename.stem, str(filename), 1 if valid else 0),
        )
        conn.commit()


def _folded(work: pathlib.Path, script) -> list[str]:
    return sorted(p.name for p in (work / "generated_samples").glob("*.wav"))


# -- the household's recordings reach the positive set ------------------------


def test_every_residents_clips_are_folded_in_oversampled(script, tmp_path, samples, db):
    # Per household, not per resident: one model wakes the box for everyone.
    used, residents = script.fold_real_positives(tmp_path, samples, db, 3)

    assert (used, residents) == (4, 2)
    names = _folded(tmp_path, script)
    assert len(names) == 4 * 3
    assert "real_alex_sample_alex_1_000.wav" in names
    assert "real_marco_sample_marco_1_002.wav" in names


def test_folded_names_cannot_collide_with_the_synthetic_sequence(
    script, tmp_path, samples, db
):
    # generate_positives writes "%06d.wav" into the same dir; a collision would
    # silently drop a synthetic positive (or a real one).
    out = tmp_path / "generated_samples"
    out.mkdir()
    (out / "000000.wav").write_bytes(b"synthetic")

    script.fold_real_positives(tmp_path, samples, db, 2)

    assert (out / "000000.wav").read_bytes() == b"synthetic"
    assert all(n.startswith(script.REAL_PREFIX) for n in _folded(tmp_path, script)[1:])


def test_the_log_says_how_many_clips_from_how_many_residents(
    script, tmp_path, samples, db, capsys
):
    # A run's log must answer "did it use my recordings?" without guessing.
    script.fold_real_positives(tmp_path, samples, db, 5)

    out = capsys.readouterr().out
    assert "Folded 4 real clip(s) from 2 resident(s)" in out
    assert "as 20 positives (oversample x5)" in out


# -- what must never reach the positive set -----------------------------------


def test_a_recorder_rejected_recording_is_skipped(
    script, tmp_path, samples, db, capsys
):
    # is_valid comes from the recorder (the STT transcript didn't say Solaris);
    # that judgement is read, not re-derived.
    _mark(db, samples / "alex" / "sample_alex_2.wav", valid=False)
    _mark(db, samples / "alex" / "sample_alex_1.wav", valid=True)

    used, _ = script.fold_real_positives(tmp_path, samples, db, 1)

    assert used == 3
    assert "real_alex_sample_alex_2_000.wav" not in _folded(tmp_path, script)
    assert (
        "skip alex/sample_alex_2.wav: recorder marked it invalid"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"rate": 8000}, "8000 Hz/1ch/16-bit, want 16 kHz mono 16-bit"),
        ({"channels": 2}, "16000 Hz/2ch/16-bit, want 16 kHz mono 16-bit"),
        ({"width": 1, "amplitude": 100}, "16000 Hz/1ch/8-bit"),
        ({"seconds": 0.1}, "too short: 0.10s"),
        ({"seconds": 6.0}, "too long: 6.00s"),
        ({"amplitude": 3}, "silent: peak"),
        ({"amplitude": 32760}, "clipped:"),
    ],
)
def test_a_clip_that_would_poison_the_set_is_skipped_with_a_reason(
    script, tmp_path, samples, db, capsys, kwargs, reason
):
    bad = write_wav(samples / "alex" / "sample_alex_9.wav", **kwargs)

    used, _ = script.fold_real_positives(tmp_path, samples, db, 1)

    assert used == 4
    assert f"real_alex_{bad.stem}_000.wav" not in _folded(tmp_path, script)
    assert reason in capsys.readouterr().out


def test_a_file_that_is_not_a_wav_at_all_is_skipped(script, tmp_path, samples, db):
    (samples / "alex" / "sample_alex_9.wav").write_bytes(b"not audio")

    used, _ = script.fold_real_positives(tmp_path, samples, db, 1)

    assert used == 4


# -- degenerate households ----------------------------------------------------


def test_a_household_with_no_recordings_still_trains_on_synthetics(
    script, tmp_path, db, capsys
):
    used, residents = script.fold_real_positives(
        tmp_path, tmp_path / "nothing-here", db, 20
    )

    assert (used, residents) == (0, 0)
    assert "synthetic positives only" in capsys.readouterr().out
    assert (tmp_path / "generated_samples").is_dir()


def test_an_unmigrated_db_does_not_stop_the_fold(script, tmp_path, samples):
    # schema-init hasn't created wakeword_samples yet: nothing is *known* bad.
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    assert script.rejected_samples(tmp_path / "missing.db") == set()
    assert script.rejected_samples(empty) == set()
    assert script.fold_real_positives(tmp_path, samples, empty, 1)[0] == 4


def test_oversample_zero_folds_nothing(script, tmp_path, samples, db):
    assert script.fold_real_positives(tmp_path, samples, db, 0) == (0, 0)
    assert _folded(tmp_path, script) == []


def test_a_deleted_recording_does_not_survive_the_next_run(
    script, tmp_path, samples, db
):
    # The wizard lets residents delete a bad take; the work dir is a volume that
    # outlives the run, so last run's copies must go before the new fold.
    script.fold_real_positives(tmp_path, samples, db, 2)
    (samples / "alex" / "sample_alex_3.wav").unlink()

    used, _ = script.fold_real_positives(tmp_path, samples, db, 2)

    assert used == 3
    assert not [n for n in _folded(tmp_path, script) if "sample_alex_3" in n]


# -- the paths have to line up inside the container ---------------------------


def test_the_trainer_hands_the_queue_db_to_the_training_script(trainer, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(trainer, "_run", lambda cmd, cwd=None: seen.append(cmd))
    monkeypatch.setattr(trainer, "DB_PATH", "/var/lib/solaris/solaris.db")
    monkeypatch.setattr(trainer, "REAL_OVERSAMPLE", "7")

    trainer.train(pathlib.Path("/work"))

    cmd = seen[0]
    assert cmd[cmd.index("--samples-db") + 1] == "/var/lib/solaris/solaris.db"
    assert cmd[cmd.index("--real-oversample") + 1] == "7"


def test_the_oversample_default_is_the_scripts_own(trainer, monkeypatch):
    # Repeating the number in the worker would let the two drift.
    seen: list[list[str]] = []
    monkeypatch.setattr(trainer, "_run", lambda cmd, cwd=None: seen.append(cmd))
    monkeypatch.setattr(trainer, "REAL_OVERSAMPLE", "")

    trainer.train(pathlib.Path("/work"))

    assert "--real-oversample" not in seen[0]


def test_the_sample_paths_are_the_ones_the_quadlet_mounts(script, trainer):
    # The failure mode this codebase keeps hitting: a path that only exists
    # outside the container. The pod writes the recordings next to solaris.db on
    # the host dir the trainer Quadlet mounts at /var/lib/solaris.
    pd = _load("solaris_pd", REPO / "templates" / "solaris" / "post-deploy.py")
    unit = pd.render_wakeword_trainer_unit("/mnt/data")
    # `:z`, never `:Z` (#1271): the pod shares this volume and `podman kube play`
    # never relabels, so a private relabel here locks the pod out of solaris.db.
    assert "Volume=/mnt/data/solarisbay:/var/lib/solaris:z" in unit
    assert str(script.DEFAULT_SAMPLES_DB) == "/var/lib/solaris/solaris.db"
    assert trainer.DB_PATH.startswith("/var/lib/solaris/")


def test_the_default_samples_dir_is_where_the_engine_writes(script):
    # solaris_chat.wakeword_samples_store.sample_path: <db dir>/wakeword/
    # user_samples/<uid>/sample_<uid>_<n>.wav
    store = (
        REPO / "solaris-chat" / "src" / "solaris_chat" / "wakeword_samples_store.py"
    ).read_text("utf-8")
    for part in script.USER_SAMPLES_SUBPATH:
        assert f'"{part}"' in store
    root = script.DEFAULT_SAMPLES_DB.parent.joinpath(*script.USER_SAMPLES_SUBPATH)
    assert str(root) == "/var/lib/solaris/wakeword/user_samples"
