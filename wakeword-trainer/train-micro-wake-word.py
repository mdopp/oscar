#!/usr/bin/env python3
"""Reproducibly train the "Solaris" microWakeWord wake-word model.

This is the offline producer for the *on-device* "Solaris" wake word that the
household's HA Voice PE (ESP32-S3) actually runs. The Voice PE detects its wake
word locally via **microWakeWord** — a different framework and model format from
the server-side **openWakeWord** model produced by `scripts/train-wake-word.py`.
The openWakeWord `solaris.tflite` is only consumed by streaming Wyoming
satellites and never wakes the Voice PE (see #525), so this is a from-scratch
microWakeWord build, not a reuse/convert of that asset.

Framework: kahrendt/microWakeWord (TensorFlow; INT8 streaming KWS for ESP32-S3).
Output: a quantized *streaming* `.tflite` + an ESPHome v2 manifest JSON — the two
files the ESPHome `micro_wake_word:` component consumes. Phase 2 (flash) is a
separate, ESPHome-builder-gated step; this script only produces the model asset.

Like the openWakeWord producer, this is deliberately NOT run by CI / the image
build: it needs Piper, TensorFlow-GPU, datasets and ~minutes-to-hours of GPU
time. It is baked into the `solaris-wakeword-trainer` image and driven by
`trainer.py`, which claims a queued `wakeword_training_runs` row (#1066),
provisions the work dir and runs this script. Phase 2 (flash) stays manual.

  RECIPE  (one GPU box, podman; tensorflow:2.18-gpu)
  -------------------------------------------------
  This script runs ALL phases end to end inside the container. It expects a
  work dir (the trainer Quadlet's /work volume) holding the two sources —
  `trainer.py` copies them in from the image, or clone them by hand:

    git clone https://github.com/kahrendt/microWakeWord
    git clone https://github.com/rhasspy/piper-sample-generator
    # German Piper voices into piper-sample-generator/voices/ (see VOICES below)

  then, inside `tensorflow/tensorflow:2.18.0-gpu` (--device nvidia.com/gpu=all
  AND --security-opt label=disable — without label=disable SELinux blocks
  /dev/nvidia* and TF silently falls back to CPU; --shm-size=8g) with
  microWakeWord + piper-sample-generator + their deps pip-installed:

    python train-micro-wake-word.py --work /work --steps 12000

  Run with TF_FORCE_GPU_ALLOW_GROWTH=true so TF doesn't grab all 16 GB VRAM —
  ollama/whisper/kokoro share this GPU.

  Phases (each is idempotent on its output dir):
    1. generate  — synth German "Solaris" utterances with the German Piper
       voices (multi-speaker; varied length/noise scales). German is
       load-bearing: trained_languages=["de"]; English pronunciation tanks
       recall on a German speaker.
    2. real      — fold the household's OWN "Solaris" recordings (the ten the
       wizard collects per resident, #1074) into the same positive dir, so they
       ride the identical augmentation path. Per household, not per resident:
       one model wakes the box for everyone.
    3. features  — augment (RIR + ambient/music background) and compute the
       streaming spectrogram feature mmaps (training/validation/testing).
    4. negatives — fetch microWakeWord's pre-generated negative spectrogram
       datasets (speech / dinner_party / no_speech + *_eval) from HF
       kahrendt/microwakeword (no 17 GB ACAV download — these are ready-made).
    5. train     — mixednet, then convert+quantize to the streaming INT8 tflite
       and run the ROC test (recall + false-accepts/hour at each cutoff).
    6. export    — pick the probability cutoff at a target false-accepts/hour,
       estimate the tensor arena, and write solaris.tflite + solaris.json.

  VOICES (German, into piper-sample-generator/voices/):
    thorsten/high           (de_DE-thorsten-high)        — single clear male
    thorsten_emotional/medium (de_DE-thorsten_emotional) — many emotions/speakers
    eva_k/x_low, kerstin, karlsson, pavoque, ramona ...   — add for diversity
  Each voice is one .onnx + matching .onnx.json from
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/<voice>/...
  Pass every voice as a repeated --voice; multi-speaker voices fan out via
  --max-speakers.

  TUNING (the real cost is iteration, per upstream):
    --positive-samples  more = better recall, slower gen
    --real-oversample   how many copies of each resident recording join the
                        positive set (see DEFAULT_REAL_OVERSAMPLE — unverified)
    --steps             more training steps usually help until it plateaus
    negative/positive class weights + sampling weights in the written yaml are
    the biggest quality levers; bump negative_class_weight if it false-accepts.
"""

from __future__ import annotations

import argparse
import array
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import wave

import yaml

WAKE_PHRASE = "Solaris"
WAKE_WORD_ID = "solaris"
# German phonetic spellings improve Piper pronunciation of "Solaris" for a
# de_DE voice; the plain word is kept too so we cover both stress patterns.
TARGET_SPELLINGS = ["Solaris", "Solahris", "Solahriss"]

# German Piper voices to synthesize positives with. Filenames as downloaded into
# <work>/piper-sample-generator/voices/. Multi-speaker voices fan out speakers.
DEFAULT_VOICES = [
    "de_DE-thorsten-high.onnx",
    "de_DE-thorsten_emotional-medium.onnx",
    "de_DE-eva_k-x_low.onnx",
    "de_DE-kerstin-low.onnx",
    "de_DE-karlsson-low.onnx",
    "de_DE-ramona-low.onnx",
    "de_DE-pavoque-low.onnx",
]

# Where the engine and the gatekeeper write the residents' own recordings, and
# the queue DB that says which of them the recorder judged usable. Both the pod
# and the trainer Quadlet mount the same host dir at /var/lib/solaris, so these
# paths mean the same file in both containers.
DEFAULT_SAMPLES_DB = pathlib.Path("/var/lib/solaris/solaris.db")
USER_SAMPLES_SUBPATH = ("wakeword", "user_samples")

# Resident recordings are copied into the synthetic positives dir under this
# prefix — it can never collide with generate_positives' "%06d.wav" sequence.
REAL_PREFIX = "real_"

# What a usable recording looks like. Same contract the browser recorder and the
# gatekeeper write (#1081): 16 kHz mono 16-bit, one spoken word.
SAMPLE_RATE = 16000
MIN_FRAMES = SAMPLE_RATE // 4  # 0.25 s
MAX_FRAMES = SAMPLE_RATE * 4  # 4 s
# int16 peak below this is a muted mic or room tone, not a spoken wake word.
SILENCE_PEAK = 500
CLIPPED_LEVEL = 32700
MAX_CLIPPED_SHARE = 0.01

# How many copies of each resident recording join the positive set.
#
# UNVERIFIED — nobody has yet run a full training with real samples, and this
# ratio can only be judged by how the resulting model behaves on the household's
# voices. It is a starting point, not a tuned constant. The reasoning: ten
# recordings against --positive-samples 2000 is 0.5% of the positive set, a
# rounding error the model never learns from; x20 makes them ~9%. Copies are not
# dead weight — the augmenter draws RIR/background/EQ/gain per clip, so each
# copy becomes a different training example.
DEFAULT_REAL_OVERSAMPLE = 20


def _run(cmd: list[str], cwd: pathlib.Path | None = None) -> None:
    sys.stdout.write("+ " + " ".join(cmd) + "\n")
    sys.stdout.flush()
    subprocess.run(cmd, cwd=cwd, check=True)


def generate_positives(work: pathlib.Path, voices: list[str], n: int) -> pathlib.Path:
    """Synthesize German 'Solaris' clips into <work>/generated_samples."""
    psg = work / "piper-sample-generator"
    out = work / "generated_samples"
    out.mkdir(parents=True, exist_ok=True)
    voice_args: list[str] = []
    for v in voices:
        p = psg / "voices" / v
        if p.exists():
            voice_args += ["--model", str(p)]
    if not voice_args:
        raise SystemExit(
            f"No German Piper voices found in {psg / 'voices'}. Download at least "
            f"one (.onnx + .onnx.json) before generating positives."
        )
    per_phrase = max(1, n // len(TARGET_SPELLINGS))
    idx = 0
    for phrase in TARGET_SPELLINGS:
        phrase_dir = out / f"p{idx}"
        phrase_dir.mkdir(exist_ok=True)
        _run(
            [
                sys.executable,
                "-m",
                "piper_sample_generator",
                phrase,
                "--max-samples",
                str(per_phrase),
                "--batch-size",
                "10",
                *voice_args,
                "--length-scales",
                "0.85",
                "1.0",
                "1.15",
                "1.3",
                "--noise-scales",
                "0.667",
                "0.85",
                "1.0",
                "--output-dir",
                str(phrase_dir),
            ],
            cwd=psg,
        )
        # flatten into the single samples dir with unique names
        for wav in sorted(phrase_dir.glob("*.wav")):
            wav.rename(out / f"{idx:06d}.wav")
            idx += 1
        shutil.rmtree(phrase_dir, ignore_errors=True)
    sys.stdout.write(f"Generated {idx} positive clips -> {out}\n")
    return out


def rejected_samples(db_path: pathlib.Path) -> set[str]:
    """Recording *filenames* the recorder already marked `is_valid = 0`.

    Keyed by basename, not by the stored absolute path: the path is written by
    whichever container recorded the clip, and matching on the name (which
    carries the uid: `sample_<uid>_<n>.wav`) survives any prefix difference.
    A missing DB/table means nothing is known to be bad, not that nothing is.
    """
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT filename FROM wakeword_samples WHERE is_valid = 0"
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return {pathlib.PurePath(r[0]).name for r in rows if r[0]}


def reject_reason(wav_path: pathlib.Path) -> str | None:
    """Why this recording must stay out of the positive set, or None if it may
    join it. A silent, clipped or misencoded clip teaches the model that the
    wake word sounds like nothing at all, so it is worse than one clip fewer."""
    try:
        with wave.open(str(wav_path), "rb") as wav:
            fmt = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            if fmt != (1, 2, SAMPLE_RATE):
                return (
                    f"{fmt[2]} Hz/{fmt[0]}ch/{fmt[1] * 8}-bit, want 16 kHz mono 16-bit"
                )
            frames = wav.getnframes()
            pcm = wav.readframes(frames)
    except (wave.Error, EOFError, OSError) as err:
        return f"unreadable: {type(err).__name__}: {err}"
    if frames < MIN_FRAMES:
        return f"too short: {frames / SAMPLE_RATE:.2f}s"
    if frames > MAX_FRAMES:
        return f"too long: {frames / SAMPLE_RATE:.2f}s"
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % samples.itemsize])
    if sys.byteorder == "big":
        samples.byteswap()  # WAV PCM is little-endian
    if not samples:
        return "no audio data"
    peak = max(max(samples), -min(samples))
    if peak < SILENCE_PEAK:
        return f"silent: peak {peak}"
    clipped = sum(1 for s in samples if s >= CLIPPED_LEVEL or s <= -CLIPPED_LEVEL)
    if clipped > len(samples) * MAX_CLIPPED_SHARE:
        return f"clipped: {clipped / len(samples):.1%} of frames at full scale"
    return None


def fold_real_positives(
    work: pathlib.Path,
    samples_root: pathlib.Path,
    db_path: pathlib.Path,
    oversample: int,
) -> tuple[int, int]:
    """Copy every resident's own recordings into the synthetic positives dir.

    The wake word is per household, not per resident — one model wakes the box
    for everyone — so this takes every uid's samples, not only the one that
    triggered the run. Returns (clips folded in, residents they came from);
    (0, 0) when nobody has recorded anything, which trains on synthetics alone.

    The feature phase splits the positives train/validation/test at random, so
    copies of one recording land on both sides: the ROC table's recall is
    optimistic for the household's own voices. Judge the model on the box.
    """
    out = work / "generated_samples"
    out.mkdir(parents=True, exist_ok=True)
    # A recording the resident deleted must not survive on the work volume from
    # an earlier run, so the previous fold is dropped before the new one.
    for stale in out.glob(f"{REAL_PREFIX}*.wav"):
        stale.unlink()
    if oversample < 1 or not samples_root.is_dir():
        sys.stdout.write(
            f"No resident recordings folded in from {samples_root} "
            f"(oversample x{oversample}) — training on synthetic positives only\n"
        )
        return 0, 0

    rejected = rejected_samples(db_path)
    residents: set[str] = set()
    used = 0
    for wav in sorted(samples_root.glob("*/*.wav")):
        uid = wav.parent.name
        if wav.name in rejected:
            sys.stdout.write(f"  skip {uid}/{wav.name}: recorder marked it invalid\n")
            continue
        reason = reject_reason(wav)
        if reason is not None:
            sys.stdout.write(f"  skip {uid}/{wav.name}: {reason}\n")
            continue
        for copy in range(oversample):
            shutil.copyfile(wav, out / f"{REAL_PREFIX}{uid}_{wav.stem}_{copy:03d}.wav")
        residents.add(uid)
        used += 1
    sys.stdout.write(
        f"Folded {used} real clip(s) from {len(residents)} resident(s) into {out} "
        f"as {used * oversample} positives (oversample x{oversample})\n"
    )
    return used, len(residents)


def build_features(work: pathlib.Path, mww: pathlib.Path) -> None:
    """Augment positives and write the streaming spectrogram feature mmaps."""
    script = work / "_build_features.py"
    script.write_text(_FEATURES_SCRIPT)
    _run([sys.executable, str(script)], cwd=work)


def fetch_negatives(work: pathlib.Path) -> None:
    """Download microWakeWord's pre-generated negative spectrogram datasets."""
    out = work / "negative_datasets"
    if (out / "speech").exists():
        sys.stdout.write("negatives already present, skipping download\n")
        return
    out.mkdir(parents=True, exist_ok=True)
    root = "https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main/"
    for fname in (
        "dinner_party.zip",
        "dinner_party_eval.zip",
        "no_speech.zip",
        "speech.zip",
    ):
        zp = out / fname
        _run(["wget", "-q", "-O", str(zp), root + fname])
        _run(["unzip", "-q", "-o", str(zp), "-d", str(out)])
        zp.unlink()


def write_training_config(work: pathlib.Path, steps: int) -> pathlib.Path:
    cfg = {
        "window_step_ms": 10,
        "train_dir": str(work / "trained_models" / "wakeword"),
        "features": [
            {
                "features_dir": str(work / "generated_augmented_features"),
                "sampling_weight": 2.0,
                "penalty_weight": 1.0,
                "truth": True,
                "truncation_strategy": "truncate_start",
                "type": "mmap",
            },
            {
                "features_dir": str(work / "negative_datasets" / "speech"),
                "sampling_weight": 10.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(work / "negative_datasets" / "dinner_party"),
                "sampling_weight": 10.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(work / "negative_datasets" / "no_speech"),
                "sampling_weight": 5.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(work / "negative_datasets" / "dinner_party_eval"),
                "sampling_weight": 0.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "split",
                "type": "mmap",
            },
        ],
        "training_steps": [steps],
        "positive_class_weight": [1],
        "negative_class_weight": [20],
        "learning_rates": [0.001],
        "batch_size": 128,
        "time_mask_max_size": [0],
        "time_mask_count": [0],
        "freq_mask_max_size": [0],
        "freq_mask_count": [0],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "target_minimization": 0.9,
        "minimization_metric": None,
        "maximization_metric": "average_viable_recall",
    }
    path = work / "training_parameters.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def train(work: pathlib.Path, mww: pathlib.Path, cfg: pathlib.Path) -> None:
    _run(
        [
            sys.executable,
            "-m",
            "microwakeword.model_train_eval",
            f"--training_config={cfg}",
            "--train",
            "1",
            "--restore_checkpoint",
            "1",
            "--test_tflite_streaming_quantized",
            "1",
            "--use_weights",
            "best_weights",
            "mixednet",
            "--pointwise_filters",
            "64,64,64,64",
            "--repeat_in_block",
            "1, 1, 1, 1",
            "--mixconv_kernel_sizes",
            "[5], [7,11], [9,15], [23]",
            "--residual_connection",
            "0,0,0,0",
            "--first_conv_filters",
            "32",
            "--first_conv_kernel_size",
            "5",
            "--stride",
            "3",
        ],
        cwd=mww,
    )


def export(work: pathlib.Path, out_tflite: pathlib.Path, cutoff: float) -> None:
    """Copy the quantized streaming tflite out + write the ESPHome v2 manifest."""
    src = (
        work
        / "trained_models"
        / "wakeword"
        / "tflite_stream_state_internal_quant"
        / "stream_state_internal_quant.tflite"
    )
    if not src.exists():
        raise SystemExit(f"trained tflite not found at {src}")
    out_tflite.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out_tflite)

    arena = _estimate_arena()
    manifest = {
        "type": "micro",
        "wake_word": WAKE_PHRASE,
        "author": "Solaris",
        "website": "https://github.com/mdopp/solarisbay",
        "model": out_tflite.name,
        "trained_languages": ["de"],
        "version": 2,
        "micro": {
            "probability_cutoff": round(cutoff, 3),
            "sliding_window_size": 5,
            "feature_step_size": 10,
            "tensor_arena_size": arena,
            "minimum_esphome_version": "2024.7.0",
        },
    }
    manifest_path = out_tflite.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    sys.stdout.write(f"Wrote {out_tflite} ({out_tflite.stat().st_size} B)\n")
    sys.stdout.write(f"Wrote {manifest_path}\n{json.dumps(manifest, indent=2)}\n")


def _estimate_arena() -> int:
    """Tensor-arena hint; ESPHome computes/bumps the real value at flash time.

    There is no public TFLite API for the micro arena requirement, so we ship a
    safe okay_nabu-class default. If ESPHome rejects it at flash (Phase 2) it
    reports the exact bytes needed; bump this field then.
    """
    return 30000


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--work", default="/work", help="box training work dir")
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("templates/solaris/wakeword/solaris-micro.tflite"),
        help="where the produced streaming tflite is shipped",
    )
    ap.add_argument("--positive-samples", type=int, default=2000)
    ap.add_argument(
        "--samples-db",
        type=pathlib.Path,
        default=DEFAULT_SAMPLES_DB,
        help="solaris.db — says which resident recordings the recorder rejected",
    )
    ap.add_argument(
        "--user-samples",
        type=pathlib.Path,
        default=None,
        help="dir of <uid>/*.wav resident recordings (default: next to --samples-db)",
    )
    ap.add_argument(
        "--real-oversample",
        type=int,
        default=DEFAULT_REAL_OVERSAMPLE,
        help="copies of each resident recording in the positive set (0 disables)",
    )
    ap.add_argument("--steps", type=int, default=12000)
    ap.add_argument("--cutoff", type=float, default=0.95)
    ap.add_argument(
        "--voice",
        action="append",
        default=None,
        help="German Piper voice .onnx filename (repeatable; default set if omitted)",
    )
    ap.add_argument(
        "--phase",
        choices=["all", "generate", "real", "features", "negatives", "train", "export"],
        default="all",
    )
    args = ap.parse_args(argv)

    work = pathlib.Path(args.work)
    mww = work / "microWakeWord"
    voices = args.voice or DEFAULT_VOICES
    samples_root = args.user_samples or args.samples_db.parent.joinpath(
        *USER_SAMPLES_SUBPATH
    )

    if args.phase in ("all", "generate"):
        generate_positives(work, voices, args.positive_samples)
    if args.phase in ("all", "real"):
        fold_real_positives(work, samples_root, args.samples_db, args.real_oversample)
    if args.phase in ("all", "features"):
        build_features(work, mww)
    if args.phase in ("all", "negatives"):
        fetch_negatives(work)
    if args.phase in ("all", "train"):
        cfg = write_training_config(work, args.steps)
        train(work, mww, cfg)
    if args.phase in ("all", "export"):
        export(work, args.out, args.cutoff)
    return 0


# Runs inside <work>; relies on microWakeWord being importable. Kept as a string
# so the producer is a single committed file (the box has no editor).
_FEATURES_SCRIPT = """\
import glob
import os
from mmap_ninja.ragged import RaggedMmap
from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration

def _nonempty(dirs):
    return [d for d in dirs if glob.glob(os.path.join(d, "**", "*.wav"), recursive=True)]

bg = _nonempty(["fma_16k", "audioset_16k"])
rir = _nonempty(["mit_rirs"])

clips = Clips(
    input_directory="generated_samples",
    file_pattern="*.wav",
    max_clip_duration_s=None,
    remove_silence=False,
    random_split_seed=10,
    split_count=0.1,
)
augmenter = Augmentation(
    augmentation_duration_s=3.2,
    augmentation_probabilities={
        "SevenBandParametricEQ": 0.1,
        "TanhDistortion": 0.1,
        "PitchShift": 0.1,
        "BandStopFilter": 0.1,
        "AddColorNoise": 0.1,
        "AddBackgroundNoise": 0.75 if bg else 0.0,
        "Gain": 1.0,
        "RIR": 0.5 if rir else 0.0,
    },
    impulse_paths=rir,
    background_paths=bg,
    background_min_snr_db=-5,
    background_max_snr_db=10,
    min_jitter_s=0.195,
    max_jitter_s=0.205,
)

out_root = "generated_augmented_features"
os.makedirs(out_root, exist_ok=True)
for split, split_name, repetition, slide in (
    ("training", "train", 2, 10),
    ("validation", "validation", 1, 10),
    ("testing", "test", 1, 1),
):
    out_dir = os.path.join(out_root, split)
    os.makedirs(out_dir, exist_ok=True)
    spectrograms = SpectrogramGeneration(
        clips=clips, augmenter=augmenter, slide_frames=slide, step_ms=10
    )
    RaggedMmap.from_generator(
        out_dir=os.path.join(out_dir, "wakeword_mmap"),
        sample_generator=spectrograms.spectrogram_generator(
            split=split_name, repeat=repetition
        ),
        batch_size=100,
        verbose=True,
    )
"""


if __name__ == "__main__":
    sys.exit(main())
