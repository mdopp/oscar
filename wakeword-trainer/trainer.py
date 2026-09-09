#!/usr/bin/env python3
"""Solaris wakeword trainer — the GPU worker behind trigger_wakeword_training (#1066).

The engine can never start microWakeWord training itself: the chat image is a
slim Python base with no TensorFlow, no GPU and no training script, and a pod
container has no route to the host's podman. So the engine enqueues a
`wakeword_training_runs` row and this container — a GPU companion Quadlet next
to whisper and Kokoro — claims it, runs `train-micro-wake-word.py` and writes
the result back.

The DB handshake mirrors the gatekeeper's `enroll_stash`: one atomic
`queued -> running` claim so two pollers can never take the same run, a terminal
`done`/`failed` write-back, and a missing table/DB makes every op a no-op so the
poller idles quietly until schema-init has migrated.

Phase 2 — flashing the produced `.tflite` onto the Voice PE through the ESPHome
builder — is NOT part of this worker (see the training script's own docstring).
A `done` row means the model asset exists, not that the wake word is live.

Stdlib only: TensorFlow lives in the image and is reached through the training
script's own subprocess, never imported here.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB_PATH = os.environ.get("SOLARIS_DB_PATH", "/var/lib/solaris/solaris.db")
WORK_DIR = os.environ.get("WAKEWORD_WORK_DIR", "/work")
POLL_SECONDS = int(os.environ.get("WAKEWORD_POLL_SECONDS", "30"))
TRAINING_STEPS = os.environ.get("WAKEWORD_TRAINING_STEPS", "12000")
# Copies of each resident recording in the positive set (#1074). Empty = leave
# the training script's own default; duplicating the number here would let the
# two drift.
REAL_OVERSAMPLE = os.environ.get("WAKEWORD_REAL_OVERSAMPLE", "")

# The image ships the sources under /opt, but the training script's recipe
# expects them inside the work dir — and the work dir is the volume that
# survives a container restart, so the multi-gigabyte artefacts they accumulate
# (features, negative corpora, checkpoints) are not rebuilt every time.
IMAGE_SOURCES = Path(os.environ.get("WAKEWORD_IMAGE_SOURCES", "/opt"))
SOURCE_REPOS = ("microWakeWord", "piper-sample-generator")
TRAIN_SCRIPT = IMAGE_SOURCES / "train-micro-wake-word.py"

# The German Piper voices train-micro-wake-word.py synthesizes its positives
# with; the HuggingFace path is derivable from the filename
# (de_DE-<voice>-<quality>.onnx -> <voice>/<quality>/).
PIPER_VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE"
VOICES = (
    "de_DE-thorsten-high.onnx",
    "de_DE-thorsten_emotional-medium.onnx",
    "de_DE-eva_k-x_low.onnx",
    "de_DE-kerstin-low.onnx",
    "de_DE-karlsson-low.onnx",
    "de_DE-ramona-low.onnx",
    "de_DE-pavoque-low.onnx",
)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

log = logging.getLogger("wakeword-trainer")

# The container runs this script as PID 1, and a PID 1 without an explicit
# handler ignores SIGTERM: podman then waits 10s and SIGKILLs, which systemd
# reports as exit 137 / `failed` on every planned stop by the GPU lease (#1407).
_stopping = False
_child: subprocess.Popen | None = None


class _Stopped(Exception):
    """A stop signal arrived while a run was in flight."""


def _on_stop(signum, frame) -> None:
    global _stopping
    _stopping = True
    child = _child
    if child is not None and child.poll() is None:
        child.terminate()


def _sleep(seconds: float) -> None:
    """Idle wait that a stop signal cuts short — PEP 475 makes a plain
    `time.sleep` resume after the handler, which would outlast podman's 10s."""
    deadline = time.monotonic() + seconds
    while not _stopping:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def claim_queued_run(db_path: str) -> tuple[int, str] | None:
    """Take the oldest queued run and mark it `running`, returning (id, uid).
    None when there is nothing queued or the table/DB is missing. The claim is a
    conditional UPDATE inside BEGIN IMMEDIATE, so a second poller can never take
    a run this one already holds."""
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, uid FROM wakeword_training_runs"
                " WHERE status = ? ORDER BY id LIMIT 1",
                (STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            claimed = conn.execute(
                "UPDATE wakeword_training_runs"
                " SET status = ?, started_at = datetime('now')"
                " WHERE id = ? AND status = ?",
                (STATUS_RUNNING, row["id"], STATUS_QUEUED),
            ).rowcount
            conn.commit()
    except sqlite3.OperationalError:
        return None
    if not claimed:
        return None
    return int(row["id"]), str(row["uid"])


def finish_run(
    db_path: str, run_id: int, *, ok: bool, result: str, model_path: str = ""
) -> None:
    """Write the terminal status for a run: `done` with the produced model, or
    `failed` with a short reason. Best-effort, like the gatekeeper's."""
    if not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE wakeword_training_runs"
                " SET status = ?, finished_at = datetime('now'),"
                "     result = ?, model_path = ?"
                " WHERE id = ?",
                (
                    STATUS_DONE if ok else STATUS_FAILED,
                    result,
                    model_path or None,
                    run_id,
                ),
            )
            conn.commit()
    except sqlite3.OperationalError:
        return


def fail_orphaned_runs(db_path: str) -> int:
    """Fail every row still `running` at startup, returning how many.

    One trainer runs per box, so a `running` row can only be this container's
    own work from before the restart — the process that held it is gone.
    Re-queueing an hours-long GPU job that already died once would crash-loop
    the box; leaving the row `running` would make the resident wait forever.
    """
    if not Path(db_path).exists():
        return 0
    try:
        with _connect(db_path) as conn:
            changed = conn.execute(
                "UPDATE wakeword_training_runs"
                " SET status = ?, finished_at = datetime('now'), result = ?"
                " WHERE status = ?",
                (
                    STATUS_FAILED,
                    "Trainer wurde neu gestartet, bevor der Lauf fertig war",
                    STATUS_RUNNING,
                ),
            ).rowcount
            conn.commit()
    except sqlite3.OperationalError:
        return 0
    return int(changed)


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    global _child
    log.info("+ %s", " ".join(cmd))
    if _stopping:
        raise _Stopped()
    _child = subprocess.Popen(cmd, cwd=cwd)
    try:
        code = _child.wait()
    finally:
        _child = None
    if _stopping:
        raise _Stopped()
    if code:
        raise subprocess.CalledProcessError(code, cmd)


def download_voices(dest: Path) -> None:
    """Fetch the German Piper voices into the sample generator's voices dir.
    Idempotent: a voice already on the volume is not re-downloaded."""
    dest.mkdir(parents=True, exist_ok=True)
    for voice in VOICES:
        _, name, quality = voice[: -len(".onnx")].split("-")
        for suffix in ("", ".json"):
            target = dest / (voice + suffix)
            if target.exists():
                continue
            part = target.with_name(target.name + ".part")
            _run(
                [
                    "wget",
                    "-q",
                    "-O",
                    str(part),
                    f"{PIPER_VOICES_URL}/{name}/{quality}/{voice}{suffix}",
                ]
            )
            part.replace(target)


def provision_work(work: Path) -> None:
    """Lay out the work dir the training script expects. Idempotent.

    piper-sample-generator's torch/piper-tts stack is ~3 GB of CUDA wheels for a
    dependency only the positive-sample phase needs, so it is installed here
    rather than baked into an image that is already TensorFlow-GPU-sized. The
    negative spectrogram corpora are fetched by the script's own phase 3.
    """
    work.mkdir(parents=True, exist_ok=True)
    for repo in SOURCE_REPOS:
        if not (work / repo).exists():
            shutil.copytree(IMAGE_SOURCES / repo, work / repo, symlinks=True)
    # The wheels go into site-packages, which dies with the container — so the
    # pip cache lives on the volume, or every restart re-downloads those 3 GB.
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--cache-dir",
            str(work / "pip-cache"),
            "-e",
            str(work / "piper-sample-generator"),
        ]
    )
    download_voices(work / "piper-sample-generator" / "voices")


def train(work: Path) -> Path:
    """Run all training phases and return the produced streaming tflite."""
    out = work / "out" / "solaris-micro.tflite"
    # --samples-db is the same DB this poller claims runs from, and the
    # residents' recordings sit next to it under wakeword/user_samples — one
    # host dir the pod and this container both mount at /var/lib/solaris, so
    # the paths mean the same files here as where they were written (#1074).
    cmd = [
        sys.executable,
        str(TRAIN_SCRIPT),
        "--work",
        str(work),
        "--out",
        str(out),
        "--steps",
        TRAINING_STEPS,
        "--samples-db",
        DB_PATH,
    ]
    if REAL_OVERSAMPLE:
        cmd += ["--real-oversample", REAL_OVERSAMPLE]
    _run(cmd)
    return out


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    work = Path(WORK_DIR)
    orphaned = fail_orphaned_runs(DB_PATH)
    if orphaned:
        log.warning("failed %d run(s) orphaned by a restart", orphaned)
    log.info("polling %s every %ss", DB_PATH, POLL_SECONDS)

    while not _stopping:
        claimed = claim_queued_run(DB_PATH)
        if claimed is None:
            _sleep(POLL_SECONDS)
            continue
        run_id, uid = claimed
        log.info("claimed run %d for %s", run_id, uid)
        try:
            provision_work(work)
            model = train(work)
        except _Stopped:
            # Leaving the row `running` would make the resident wait for a
            # trainer that is no longer there until the next start reaps it.
            log.info("stopped during run %d", run_id)
            finish_run(DB_PATH, run_id, ok=False, result="Trainer wurde angehalten")
            break
        except Exception as err:
            # A broken run must be one failed row, never a dead companion: the
            # container is the box's only trainer, so exiting here would take
            # the capability down until someone notices.
            reason = f"{type(err).__name__}: {err}"[:500]
            log.error("run %d failed: %s", run_id, reason)
            finish_run(DB_PATH, run_id, ok=False, result=reason)
            _sleep(POLL_SECONDS)
            continue
        manifest = model.with_suffix(".json")
        log.info("run %d done: %s", run_id, model)
        finish_run(
            DB_PATH,
            run_id,
            ok=True,
            result=f"Modell gebaut: {model.name} + {manifest.name}",
            model_path=str(model),
        )

    log.info("stopped")


if __name__ == "__main__":
    main()
