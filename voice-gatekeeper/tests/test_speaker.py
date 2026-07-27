"""Speaker-ID resolver tests — pure-numpy, no ML deps required (#937)."""

from __future__ import annotations

import importlib.util
import dataclasses
import sqlite3
from pathlib import Path

import pytest

if importlib.util.find_spec("numpy") is None:  # pragma: no cover
    pytest.skip(
        "numpy not installed — speaker tests need numpy", allow_module_level=True
    )

import numpy as np

from gatekeeper.embeddings_store import (
    EMBEDDING_DIM,
    delete_embedding,
    list_embeddings,
    list_uids,
    upsert_embedding,
)
from gatekeeper.speaker import (
    average_embeddings,
    cosine_match,
    resolve_speaker,
)


def _norm(vec: np.ndarray) -> np.ndarray:
    return (vec / np.linalg.norm(vec)).astype("<f4")


def _emb(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBEDDING_DIM, dtype="<f4")
    return _norm(v).tobytes()


def _seed_db(tmp_path: Path) -> str:
    db = tmp_path / "solaris.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE voice_embeddings (
              uid TEXT PRIMARY KEY,
              embedding BLOB NOT NULL,
              enrolled_at TEXT NOT NULL DEFAULT (datetime('now')),
              enrolled_via TEXT NOT NULL,
              sample_count INTEGER NOT NULL DEFAULT 1,
              last_seen_at TEXT
            )
            """
        )
        conn.commit()
    return str(db)


def test_cosine_match_returns_best_candidate(tmp_path: Path):
    db = _seed_db(tmp_path)
    a, b = _emb(1), _emb(2)
    upsert_embedding(db, "alice", a, sample_count=1, enrolled_via="test")
    upsert_embedding(db, "bob", b, sample_count=1, enrolled_via="test")

    candidates = list_embeddings(db)
    assert {c.uid for c in candidates} == {"alice", "bob"}

    match = cosine_match(a, candidates, threshold=0.5)
    assert match is not None
    assert match.uid == "alice"
    assert match.score == pytest.approx(1.0, abs=1e-5)
    assert match.above_threshold is True


def test_cosine_match_below_threshold_still_reports_best(tmp_path: Path):
    db = _seed_db(tmp_path)
    upsert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")
    upsert_embedding(db, "bob", _emb(2), sample_count=1, enrolled_via="test")

    far_query = _emb(99)  # different seed → low similarity
    match = cosine_match(far_query, list_embeddings(db), threshold=0.99)
    assert match is not None
    assert match.above_threshold is False  # 0.99 is impossible for random vectors


def test_resolve_speaker_falls_back_to_default(tmp_path: Path):
    db = _seed_db(tmp_path)
    # No enrolments — fall back regardless of query
    uid, match = resolve_speaker(
        _emb(1), list_embeddings(db), threshold=0.5, default_uid="guest"
    )
    assert uid == "guest"
    assert match is None

    # Enrol Alice; her own embedding should resolve to her
    a = _emb(7)
    upsert_embedding(db, "alice", a, sample_count=3, enrolled_via="test")
    uid, match = resolve_speaker(
        a, list_embeddings(db), threshold=0.5, default_uid="guest"
    )
    assert uid == "alice"
    assert match is not None and match.uid == "alice"

    # A different-seed query falls back if below threshold
    uid, match = resolve_speaker(
        _emb(8), list_embeddings(db), threshold=0.99, default_uid="guest"
    )
    assert uid == "guest"
    assert match is not None and match.above_threshold is False


def test_resolve_speaker_handles_missing_query(tmp_path: Path):
    db = _seed_db(tmp_path)
    upsert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")
    uid, match = resolve_speaker(
        None, list_embeddings(db), threshold=0.5, default_uid="guest"
    )
    assert uid == "guest"
    assert match is None


def test_average_embeddings_yields_unit_norm(tmp_path: Path):
    e1 = _emb(10)
    e2 = _emb(11)
    e3 = _emb(12)
    avg = average_embeddings([e1, e2, e3])
    arr = np.frombuffer(avg, dtype="<f4")
    assert arr.shape == (EMBEDDING_DIM,)
    assert float(np.linalg.norm(arr)) == pytest.approx(1.0, abs=1e-5)


def test_average_embeddings_rejects_zero_sum():
    half = np.ones(EMBEDDING_DIM, dtype="<f4") / np.sqrt(EMBEDDING_DIM)
    other = (-half).astype("<f4")
    with pytest.raises(ValueError):
        average_embeddings([half.tobytes(), other.tobytes()])


def test_upsert_rejects_wrong_dim(tmp_path: Path):
    db = _seed_db(tmp_path)
    with pytest.raises(ValueError):
        upsert_embedding(db, "alice", b"\x00" * 17, sample_count=1, enrolled_via="test")


def test_list_embeddings_skips_malformed_rows(tmp_path: Path):
    db = _seed_db(tmp_path)
    # Manually shove a malformed row in
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO voice_embeddings (uid, embedding, sample_count, enrolled_via) VALUES (?, ?, ?, ?)",
            ("broken", b"\x00\x00\x00", 1, "test"),
        )
        conn.commit()
    upsert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")

    embs = list_embeddings(db)
    assert {e.uid for e in embs} == {"alice"}


def test_list_embeddings_empty_when_db_missing(tmp_path: Path):
    assert list_embeddings(str(tmp_path / "nope.db")) == []
    assert list_uids(str(tmp_path / "nope.db")) == []


def test_delete_embedding_roundtrip(tmp_path: Path):
    db = _seed_db(tmp_path)
    upsert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")
    assert delete_embedding(db, "alice") is True
    assert delete_embedding(db, "alice") is False  # idempotent second call
    assert list_uids(db) == []


def test_get_extractor_disabled_via_renamed_env(monkeypatch):
    """SOLARIS_SPEAKER_ID_ENABLED unset/false -> no extractor (speaker.py:214)."""
    import gatekeeper.speaker as speaker

    monkeypatch.setattr(speaker, "_extractor_singleton", None)
    monkeypatch.setenv("SOLARIS_SPEAKER_ID_ENABLED", "off")
    assert speaker.get_extractor() is None


async def test_resolve_uid_matches_and_touches_last_seen(tmp_path, monkeypatch):
    """A populated buffer + enrolled speaker exercises the resolver's
    list_embeddings / touch_last_seen calls against solaris_db_path
    (handler.py:187 and :204)."""
    import gatekeeper.handler as handler
    from wyoming.audio import AudioChunk, AudioStart

    db = _seed_db(tmp_path)
    alice = _emb(7)
    upsert_embedding(db, "alice", alice, sample_count=1, enrolled_via="test")

    monkeypatch.setattr(
        handler,
        "settings",
        dataclasses.replace(
            handler.settings,
            speaker_id_enabled=True,
            default_uid="guest",
            speaker_id_threshold=0.5,
            solaris_db_path=db,
        ),
    )

    class _StubExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return alice

    monkeypatch.setattr(handler, "get_extractor", lambda: _StubExtractor())

    h = handler.GatekeeperHandler(None, None, object())
    h._audio_start = AudioStart(rate=16000, width=2, channels=1)
    h._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 16000)
    ]

    uid = await h._resolve_uid()
    assert uid == "alice"

    # touch_last_seen must have stamped last_seen_at for the matched uid
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT last_seen_at FROM voice_embeddings WHERE uid = ?", ("alice",)
        ).fetchone()
    assert row is not None and row[0] is not None


async def test_resolve_uid_unknown_speaker_routes_to_guest(tmp_path, monkeypatch):
    """Speaker-ID ran, embedded the audio, compared against an enrolled
    resident, and nobody cleared the threshold (a real non-match) -> the
    `guest` sentinel, NOT default_uid (#351)."""
    import gatekeeper.handler as handler
    from wyoming.audio import AudioChunk, AudioStart

    db = _seed_db(tmp_path)
    upsert_embedding(db, "alice", _emb(7), sample_count=1, enrolled_via="test")

    monkeypatch.setattr(
        handler,
        "settings",
        dataclasses.replace(
            handler.settings,
            speaker_id_enabled=True,
            default_uid="household",
            speaker_id_threshold=0.99,  # nothing random can clear this
            solaris_db_path=db,
        ),
    )

    class _StubExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return _emb(99)  # far from the enrolled embedding -> below threshold

    monkeypatch.setattr(handler, "get_extractor", lambda: _StubExtractor())

    h = handler.GatekeeperHandler(None, None, object())
    h._audio_start = AudioStart(rate=16000, width=2, channels=1)
    h._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 16000)
    ]

    assert await h._resolve_uid() == handler.GUEST_UID


async def test_resolve_uid_disabled_stays_household_not_guest(tmp_path, monkeypatch):
    """Speaker-ID OFF -> default_uid (household), never the guest sentinel:
    the default hot path must not become a guest turn (#351)."""
    import gatekeeper.handler as handler

    monkeypatch.setattr(
        handler,
        "settings",
        dataclasses.replace(
            handler.settings, speaker_id_enabled=False, default_uid="household"
        ),
    )
    h = handler.GatekeeperHandler(None, None, object())
    assert await h._resolve_uid() == "household"


async def test_resolve_uid_no_enrolments_stays_household_not_guest(
    tmp_path, monkeypatch
):
    """Speaker-ID on but no one is enrolled (no candidate to compare against)
    -> household, not guest: that's a not-attempted gap, not an unknown
    speaker (#351)."""
    import gatekeeper.handler as handler
    from wyoming.audio import AudioChunk, AudioStart

    db = _seed_db(tmp_path)  # no enrolments
    monkeypatch.setattr(
        handler,
        "settings",
        dataclasses.replace(
            handler.settings,
            speaker_id_enabled=True,
            default_uid="household",
            speaker_id_threshold=0.5,
            solaris_db_path=db,
        ),
    )

    class _StubExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return _emb(7)

    monkeypatch.setattr(handler, "get_extractor", lambda: _StubExtractor())

    h = handler.GatekeeperHandler(None, None, object())
    h._audio_start = AudioStart(rate=16000, width=2, channels=1)
    h._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 16000)
    ]
    assert await h._resolve_uid() == "household"


def test_speaker_id_enabled_from_env_default_on(monkeypatch):
    """Single predicate shared by config.speaker_id_enabled and
    speaker.get_extractor: enabled unless explicitly disabled, so a household
    box recognises residents out of the box. Guards the branch bug where the
    config forced True while get_extractor still required 1/true/yes/on, so
    speaker-ID silently did nothing."""
    from gatekeeper.config import speaker_id_enabled_from_env

    monkeypatch.delenv("SOLARIS_SPEAKER_ID_ENABLED", raising=False)
    assert speaker_id_enabled_from_env() is True
    for on in ("", "1", "true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("SOLARIS_SPEAKER_ID_ENABLED", on)
        assert speaker_id_enabled_from_env() is True
    for off in ("0", "false", "no", "off", "OFF", "False"):
        monkeypatch.setenv("SOLARIS_SPEAKER_ID_ENABLED", off)
        assert speaker_id_enabled_from_env() is False


def test_get_extractor_enabled_but_deps_missing_returns_none(monkeypatch):
    """Explicitly enabled but the ML deps are absent (the stock image with no
    speechbrain/torch) -> None, never a raise."""
    import gatekeeper.speaker as speaker

    monkeypatch.setattr(speaker, "_extractor_singleton", None)
    monkeypatch.setenv("SOLARIS_SPEAKER_ID_ENABLED", "true")
    monkeypatch.setattr(speaker, "extractor_available", lambda: False)
    assert speaker.get_extractor() is None


async def test_resolve_uid_enabled_but_no_extractor_falls_back_household(
    tmp_path, monkeypatch
):
    """Speaker-ID enabled (the default) but the extractor is unavailable ->
    household, no raise. A raised exception here would emit a 0-byte NDJSON
    stream — the Voice PE 'red ring' HTTP 500."""
    import gatekeeper.handler as handler
    from wyoming.audio import AudioChunk, AudioStart

    monkeypatch.setattr(
        handler,
        "settings",
        dataclasses.replace(
            handler.settings, speaker_id_enabled=True, default_uid="household"
        ),
    )
    monkeypatch.setattr(handler, "get_extractor", lambda: None)

    h = handler.GatekeeperHandler(None, None, object())
    h._audio_start = AudioStart(rate=16000, width=2, channels=1)
    h._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 16000)
    ]
    assert await h._resolve_uid() == "household"


def test_embedding_dim_matches_the_ecapa_model():
    """`speechbrain/spkrec-ecapa-voxceleb` emits 192 floats. If this constant
    drifts from that, `SpeechBrainExtractor.extract` fails its shape check and
    returns None for every turn — speaker-ID then silently resolves everyone to
    default_uid and enrolment can never store an embedding, with no error
    anywhere. Verified live on the box: the model returned shape (192,) while
    this said 256."""
    from gatekeeper.embeddings_store import EMBEDDING_BYTES, EMBEDDING_DIM

    assert EMBEDDING_DIM == 192
    assert EMBEDDING_BYTES == 768
