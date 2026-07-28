"""Speaker-ID resolver — k-NN over voice_embeddings, with a pluggable
embedding extractor (#937 Phase 2).

Two distinct moving parts live here:

  1. `cosine_match` / `resolve_speaker` — pure-numpy k-NN over the
     stored embeddings. Always available. Tested with synthetic
     embeddings (see tests/).
  2. `get_extractor` / `EmbeddingExtractor` — abstraction over the
     ECAPA-TDNN model that turns raw PCM into a 192-d vector. The
     default implementation imports SpeechBrain lazily; when
     SpeechBrain is not installed (the default image), it raises
     NotImplementedError on first use. Callers must check
     `extractor_available()` first and fall back to `default_uid`
     when the extractor isn't loadable.

The split keeps the resolver (and its tests) free of any ML
dependency. Operators wanting Phase-2 speaker-ID build a custom
gatekeeper image with the `speaker-id` extras installed:

    pip install /app[speaker-id]

and flip `SOLARIS_SPEAKER_ID_ENABLED=1`. Future work: a CI-built
`solaris-gatekeeper-ml` image that bakes SpeechBrain in. See #937
follow-up notes in the gatekeeper README.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Protocol

from .config import speaker_id_enabled_from_env
from .embeddings_store import EMBEDDING_DIM, VoiceEmbedding


def extractor_available() -> bool:
    """True iff the modules needed for the default ECAPA extractor
    are importable in this interpreter. Cheap — runs a couple of
    `importlib.util.find_spec` calls, no actual import."""
    for module in ("speechbrain", "torch", "numpy"):
        if importlib.util.find_spec(module) is None:
            return False
    return True


@dataclass(frozen=True)
class SpeakerMatch:
    uid: str
    score: float  # cosine similarity in [-1, 1]
    above_threshold: bool


def cosine_match(
    query_bytes: bytes,
    candidates: Iterable[VoiceEmbedding],
    *,
    threshold: float,
) -> SpeakerMatch | None:
    """Brute-force cosine over candidates. Returns the best match
    (even if below threshold, so callers can log "matched X with
    low confidence" before falling back). None when there are no
    candidates."""
    import numpy as np

    if len(query_bytes) != EMBEDDING_DIM * 4:
        raise ValueError(
            f"query embedding must be {EMBEDDING_DIM * 4} bytes, got {len(query_bytes)}"
        )
    q = np.frombuffer(query_bytes, dtype="<f4")
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return None
    q = q / q_norm

    best_uid: str | None = None
    best_score = -1.0
    for cand in candidates:
        c = cand.as_array()
        c_norm = float(np.linalg.norm(c))
        if c_norm == 0.0:
            continue
        score = float(np.dot(q, c / c_norm))
        if score > best_score:
            best_score = score
            best_uid = cand.uid

    if best_uid is None:
        return None
    return SpeakerMatch(
        uid=best_uid, score=best_score, above_threshold=best_score >= threshold
    )


def resolve_speaker(
    query_bytes: bytes | None,
    candidates: Iterable[VoiceEmbedding],
    *,
    threshold: float,
    default_uid: str,
) -> tuple[str, SpeakerMatch | None]:
    """Top-level resolver: gives the uid the Solaris Engine should be told this
    turn belongs to, plus the raw match for logging. Falls back to
    `default_uid` when no query embedding was extracted, no rows
    are enrolled, or the best match falls below threshold."""
    if query_bytes is None:
        return default_uid, None
    cands = list(candidates)
    if not cands:
        return default_uid, None
    match = cosine_match(query_bytes, cands, threshold=threshold)
    if match is None:
        return default_uid, None
    if not match.above_threshold:
        return default_uid, match
    return match.uid, match


class EmbeddingExtractor(Protocol):
    """Turn buffered audio chunks into a 192-d float32 embedding.

    The protocol is sync — extractors are CPU/GPU-bound and the
    handler calls them from an asyncio.to_thread wrapper. Returning
    None means "no usable embedding" (silence, too-short clip,
    extractor disabled) and the caller falls back to default_uid.
    """

    def extract(
        self, pcm: bytes, *, rate: int, width: int, channels: int
    ) -> bytes | None: ...


class SpeechBrainExtractor:
    """ECAPA-TDNN embedding via SpeechBrain. Loads the pretrained
    `speechbrain/spkrec-ecapa-voxceleb` weights on first use; that
    download is ~80 MB and happens once into the model cache dir.

    Stubbed out at import time so the rest of the gatekeeper still
    runs when SpeechBrain is unavailable. The handler will check
    `extractor_available()` before constructing this class.
    """

    def __init__(self, *, savedir: str | None = None) -> None:
        if not extractor_available():
            raise RuntimeError(
                "SpeechBrain / torch not installed in this image. Build a custom gatekeeper image with the [speaker-id] extras to enable Phase 2."
            )
        import torch  # noqa: F401  — import side-effects only
        from speechbrain.inference.speaker import EncoderClassifier

        self._classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=savedir
            or os.environ.get(
                "SOLARIS_SPEAKER_MODEL_CACHE", "/var/lib/solaris/models/spkrec-ecapa"
            ),
            run_opts={"device": "cpu"},  # gatekeeper sidecar has no GPU contract
        )

    def extract(
        self, pcm: bytes, *, rate: int, width: int, channels: int
    ) -> bytes | None:
        import numpy as np
        import torch

        if not pcm:
            return None
        if width != 2 or channels != 1:
            # ECAPA was trained on 16 kHz mono int16. Resampling/
            # downmixing here is doable but out of scope for the
            # framework; the gatekeeper pod only ever sees Wyoming
            # input from HA Voice PE satellites, which is 16-kHz mono.
            return None

        samples = np.frombuffer(pcm, dtype="<i2").astype("<f4") / 32768.0
        if rate != 16000:
            # Cheap linear-interp resample. Good enough for ECAPA at
            # the threshold ranges we use; a proper resampler is a
            # later optimisation.
            from math import ceil

            ratio = 16000.0 / float(rate)
            new_len = int(ceil(len(samples) * ratio))
            idx = np.linspace(0, len(samples) - 1, new_len)
            samples = np.interp(idx, np.arange(len(samples)), samples).astype("<f4")
        if len(samples) < 16000:  # < 1 s of audio — too short to embed reliably
            return None

        tensor = torch.from_numpy(samples).unsqueeze(0)
        with torch.no_grad():
            emb = (
                self._classifier.encode_batch(tensor)
                .squeeze()
                .cpu()
                .numpy()
                .astype("<f4")
            )
        if emb.shape != (EMBEDDING_DIM,):
            return None
        return emb.tobytes()


_extractor_singleton: EmbeddingExtractor | None = None


def get_extractor() -> EmbeddingExtractor | None:
    """Return the process-wide extractor instance, loading lazily.
    Returns None when speaker-id is disabled, deps are missing, or
    the model failed to load."""
    global _extractor_singleton
    if _extractor_singleton is not None:
        return _extractor_singleton
    if not speaker_id_enabled_from_env():
        return None
    if not extractor_available():
        return None
    try:
        _extractor_singleton = SpeechBrainExtractor()
    except Exception:  # noqa: BLE001 — extractor init can fail many ways
        return None
    return _extractor_singleton


def average_embeddings(samples: list[bytes]) -> bytes:
    """Mean-pool multiple per-utterance embeddings into one enrolment
    embedding, then renormalise to unit length. Used by the
    enrolment endpoint after collecting N "say your name" samples."""
    import numpy as np

    if not samples:
        raise ValueError("need at least one sample to average")
    stacked = np.stack([np.frombuffer(b, dtype="<f4") for b in samples])
    mean = stacked.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        raise ValueError("averaged embedding has zero norm — samples likely silence")
    mean = (mean / norm).astype("<f4")
    return mean.tobytes()


# How far below the group's median a leave-one-out score may sit before the
# sample counts as an outlier. Samples from one sitting share distance, room and
# time of day, so they cluster tightly; one that falls this much further off is a
# cough, a second speaker or a clipped sentence, and averaging it in drags the
# profile toward audio that isn't the resident.
LOO_OUTLIER_MARGIN = 0.15

# Leave-one-out needs a meaningful "rest of the group": with two samples each is
# measured against the other, both scores are identical, and nothing can stand
# out as the outlier.
_LOO_MIN_SAMPLES = 3

REASON_WEAK = "weak"
REASON_COLLISION = "collision"


@dataclass(frozen=True)
class EnrollmentCheck:
    ok: bool
    reason: str  # "" when ok, else REASON_WEAK / REASON_COLLISION
    min_score: float  # the score the verdict turns on — the distance to threshold


def leave_one_out_scores(samples: list[bytes]) -> list[float]:
    """Cosine of each enrolment sample against the mean of the *others*.

    Holding the sample out is the point: a sample that is itself part of the mean
    pulls the mean toward itself, hiding the very outlier we are looking for.
    Returns [] when there are too few samples for the comparison to mean
    anything.
    """
    if len(samples) < _LOO_MIN_SAMPLES:
        return []
    scores: list[float] = []
    for i, sample in enumerate(samples):
        rest = average_embeddings([s for j, s in enumerate(samples) if j != i])
        match = cosine_match(
            sample,
            [
                VoiceEmbedding(
                    uid="", embedding_bytes=rest, sample_count=len(samples) - 1
                )
            ],
            threshold=0.0,
        )
        scores.append(match.score if match is not None else -1.0)
    return scores


def drop_outliers(samples: list[bytes]) -> list[bytes]:
    """Keep the enrolment samples that agree with the rest of the sitting, so the
    profile is averaged over a coherent set. At least half the samples always
    survive (the floor is derived from the median)."""
    scores = leave_one_out_scores(samples)
    if not scores:
        return list(samples)
    floor = median(scores) - LOO_OUTLIER_MARGIN
    return [s for s, score in zip(samples, scores) if score >= floor]


def verify_enrollment(
    samples: list[bytes],
    profile: bytes,
    *,
    uid: str,
    candidates: Iterable[VoiceEmbedding],
    threshold: float,
) -> EnrollmentCheck:
    """Resolve every retained sample the way a real turn would — against the
    freshly averaged profile *and* every other enrolled resident.

    Two failures are possible and mean different things. Nothing clears the
    threshold: the profile doesn't carry yet, so collect more samples. A sample
    resolves to a *different* resident: a collision, and enrolling anyway would
    let Solaris hand one resident's data to another — a privacy failure, never a
    quality one.

    The verdict is read off `match`, never off the uid `resolve_speaker` returns:
    `default_uid` may be the enrolling resident, which would make a
    household fallback look like success.
    """
    others = list(candidates)
    # The centroid of a resident's own samples is by construction closer to each
    # of them than anyone else's row is, so the per-sample resolve below can miss
    # the collision that matters most: a new profile sitting on top of an
    # enrolled one. Every later turn would then be a coin toss between the two
    # residents, so measure the profile against them directly first.
    if others:
        rival = cosine_match(profile, others, threshold=threshold)
        if rival is not None and rival.above_threshold:
            return EnrollmentCheck(
                ok=False, reason=REASON_COLLISION, min_score=rival.score
            )
    # Own profile last: `cosine_match` keeps the first of equally-scoring
    # candidates, so a dead heat with another resident resolves to them and
    # fails closed as a collision.
    rows = [
        *others,
        VoiceEmbedding(uid=uid, embedding_bytes=profile, sample_count=len(samples)),
    ]
    reason = ""
    min_score = 1.0
    for sample in samples:
        _, match = resolve_speaker(sample, rows, threshold=threshold, default_uid="")
        if match is None:
            return EnrollmentCheck(ok=False, reason=REASON_WEAK, min_score=-1.0)
        min_score = min(min_score, match.score)
        if not match.above_threshold:
            reason = reason or REASON_WEAK
        elif match.uid != uid:
            reason = REASON_COLLISION
    return EnrollmentCheck(ok=not reason, reason=reason, min_score=min_score)
