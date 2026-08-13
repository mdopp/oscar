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
    insert_embedding,
    list_embeddings,
    list_uids,
)
from gatekeeper.speaker import (
    REASON_COLLISION,
    REASON_WEAK,
    average_embeddings,
    cosine_match,
    drop_outliers,
    leave_one_out_scores,
    resolve_speaker,
    verify_enrollment,
)


def _norm(vec: np.ndarray) -> np.ndarray:
    return (vec / np.linalg.norm(vec)).astype("<f4")


def _emb(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBEDDING_DIM, dtype="<f4")
    return _norm(v).tobytes()


def _at_cosine(base: bytes, target: float, seed: int) -> bytes:
    """A unit vector whose cosine similarity to `base` is exactly `target`.

    Lets a test state the distance it means ("these two residents both fit at
    ~0.6") instead of hoping a random seed lands there.
    """
    b = np.frombuffer(base, dtype="<f4")
    rng = np.random.default_rng(seed)
    r = rng.standard_normal(EMBEDDING_DIM, dtype="<f4")
    perp = _norm(r - float(np.dot(r, b)) * b)
    return _norm(target * b + np.sqrt(1.0 - target**2) * perp).tobytes()


def _seed_db(tmp_path: Path) -> str:
    db = tmp_path / "solaris.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE voice_embeddings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              uid TEXT NOT NULL,
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
    insert_embedding(db, "alice", a, sample_count=1, enrolled_via="test")
    insert_embedding(db, "bob", b, sample_count=1, enrolled_via="test")

    candidates = list_embeddings(db)
    assert {c.uid for c in candidates} == {"alice", "bob"}

    match = cosine_match(a, candidates, threshold=0.5)
    assert match is not None
    assert match.uid == "alice"
    assert match.score == pytest.approx(1.0, abs=1e-5)
    assert match.above_threshold is True


def test_cosine_match_below_threshold_still_reports_best(tmp_path: Path):
    db = _seed_db(tmp_path)
    insert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")
    insert_embedding(db, "bob", _emb(2), sample_count=1, enrolled_via="test")

    far_query = _emb(99)  # different seed → low similarity
    match = cosine_match(far_query, list_embeddings(db), threshold=0.99)
    assert match is not None
    assert match.above_threshold is False  # 0.99 is impossible for random vectors


def test_resolve_speaker_falls_back_to_default(tmp_path: Path):
    db = _seed_db(tmp_path)
    # No enrolments — fall back regardless of query
    uid, match = resolve_speaker(
        _emb(1), list_embeddings(db), threshold=0.5, margin=0.1, default_uid="guest"
    )
    assert uid == "guest"
    assert match is None

    # Enrol Alice; her own embedding should resolve to her
    a = _emb(7)
    insert_embedding(db, "alice", a, sample_count=3, enrolled_via="test")
    uid, match = resolve_speaker(
        a, list_embeddings(db), threshold=0.5, margin=0.1, default_uid="guest"
    )
    assert uid == "alice"
    assert match is not None and match.uid == "alice"

    # A different-seed query falls back if below threshold
    uid, match = resolve_speaker(
        _emb(8), list_embeddings(db), threshold=0.99, margin=0.1, default_uid="guest"
    )
    assert uid == "guest"
    assert match is not None and match.above_threshold is False


def test_resolve_speaker_handles_missing_query(tmp_path: Path):
    db = _seed_db(tmp_path)
    insert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")
    uid, match = resolve_speaker(
        None, list_embeddings(db), threshold=0.5, margin=0.1, default_uid="guest"
    )
    assert uid == "guest"
    assert match is None


def test_second_row_rescues_a_turn_the_first_one_refused(tmp_path: Path):
    """The point of #1084. One resident, two recording conditions: at the
    device and across the room. With only the close profile enrolled, a turn
    spoken across the room falls below threshold and becomes a guest. Adding
    the second condition as its own row — not averaged into the first — makes
    the same turn resolve to her."""
    db = _seed_db(tmp_path)
    at_the_device = _emb(1)
    across_the_room = _at_cosine(at_the_device, 0.2, seed=11)
    live_turn = _at_cosine(across_the_room, 0.9, seed=12)

    insert_embedding(db, "anna", at_the_device, sample_count=3, enrolled_via="voice")
    uid, _ = resolve_speaker(
        live_turn, list_embeddings(db), threshold=0.55, margin=0.1, default_uid="guest"
    )
    assert uid == "guest"

    insert_embedding(db, "anna", across_the_room, sample_count=3, enrolled_via="voice")
    rows = list_embeddings(db)
    assert [r.uid for r in rows] == ["anna", "anna"]
    uid, match = resolve_speaker(
        live_turn, rows, threshold=0.55, margin=0.1, default_uid="guest"
    )
    assert uid == "anna"
    assert match is not None and match.score == pytest.approx(0.9, abs=1e-3)


def test_margin_refuses_a_turn_two_residents_fit_almost_equally(tmp_path: Path):
    """The counterweight to more rows per resident. 0.62 vs 0.60 clears any
    absolute threshold and is still a coin toss between two people; attributing
    it would read one resident's notes to another."""
    db = _seed_db(tmp_path)
    query = _emb(5)
    insert_embedding(
        db,
        "anna",
        _at_cosine(query, 0.62, seed=21),
        sample_count=3,
        enrolled_via="test",
    )
    insert_embedding(
        db, "ben", _at_cosine(query, 0.60, seed=22), sample_count=3, enrolled_via="test"
    )
    rows = list_embeddings(db)

    # Threshold alone: a confident-looking Anna.
    uid, _ = resolve_speaker(
        query, rows, threshold=0.55, margin=0.0, default_uid="guest"
    )
    assert uid == "anna"

    uid, match = resolve_speaker(
        query, rows, threshold=0.55, margin=0.1, default_uid="guest"
    )
    assert uid == "guest"
    # Still reported, so the log says *why* it was refused.
    assert match is not None
    assert match.uid == "anna"
    assert match.above_threshold is True
    assert match.margin == pytest.approx(0.02, abs=1e-3)


def test_margin_measures_the_next_resident_not_the_next_row(tmp_path: Path):
    """A resident's own second-best row must never count as her rival —
    otherwise every extra row enrolled under #1084 would push her below the
    margin and un-recognise her."""
    db = _seed_db(tmp_path)
    query = _emb(5)
    insert_embedding(
        db,
        "anna",
        _at_cosine(query, 0.80, seed=31),
        sample_count=3,
        enrolled_via="test",
    )
    insert_embedding(
        db,
        "anna",
        _at_cosine(query, 0.78, seed=32),
        sample_count=3,
        enrolled_via="test",
    )
    insert_embedding(
        db, "ben", _at_cosine(query, 0.20, seed=33), sample_count=3, enrolled_via="test"
    )

    uid, match = resolve_speaker(
        query, list_embeddings(db), threshold=0.55, margin=0.1, default_uid="guest"
    )
    assert uid == "anna"
    assert match is not None
    assert match.runner_up_score == pytest.approx(0.20, abs=1e-3)


def test_margin_cannot_refuse_the_only_enrolled_resident(tmp_path: Path):
    """No other resident means nothing to confuse her with, so the margin rule
    must stay out of the way — a single-resident household is the common case."""
    db = _seed_db(tmp_path)
    query = _emb(5)
    insert_embedding(
        db,
        "anna",
        _at_cosine(query, 0.56, seed=41),
        sample_count=3,
        enrolled_via="test",
    )
    uid, match = resolve_speaker(
        query, list_embeddings(db), threshold=0.55, margin=0.5, default_uid="guest"
    )
    assert uid == "anna"
    assert match is not None and match.runner_up_score == -1.0


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


def _near(seed: int, *, base: bytes, spread: float = 0.3) -> bytes:
    """A sample from the same speaker/sitting: the base direction plus `spread`
    of a unit-length nudge, so the samples cluster the way one sitting does."""
    rng = np.random.default_rng(seed)
    nudge = _norm(rng.standard_normal(EMBEDDING_DIM, dtype="<f4"))
    return _norm(np.frombuffer(base, dtype="<f4") + spread * nudge).tobytes()


def _at_cosine(base: bytes, target: float, *, seed: int) -> bytes:
    """An embedding at exactly `target` cosine similarity to `base`: an
    independent direction, Gram-Schmidt'd off `base`, mixed back in at the right
    ratio. Lets a test place a rival profile precisely between the recognition
    threshold and the stricter collision bar."""
    rng = np.random.default_rng(seed)
    b = np.frombuffer(base, dtype="<f4")
    v = rng.standard_normal(EMBEDDING_DIM, dtype="<f4")
    orth = _norm(v - float(np.dot(v, b)) * b)
    return _norm(target * b + np.sqrt(1.0 - target * target) * orth).tobytes()


def test_leave_one_out_holds_the_sample_out(tmp_path: Path):
    """The held-out sample must not be part of the mean it is measured against —
    otherwise it pulls the mean toward itself and hides the outlier we hunt."""
    base = _emb(1)
    samples = [_near(2, base=base), _near(3, base=base), _emb(42)]  # last = stranger
    scores = leave_one_out_scores(samples)
    assert len(scores) == 3
    # The stranger scores far below the two that belong together.
    assert scores[2] < min(scores[0], scores[1]) - 0.3


def test_leave_one_out_needs_three_samples():
    """With two samples each is measured against the other, both scores are
    identical, and nothing can stand out — no verdict to give."""
    base = _emb(1)
    assert leave_one_out_scores([_near(2, base=base), _near(3, base=base)]) == []


def test_drop_outliers_removes_the_stranger_and_keeps_the_sitting():
    base = _emb(1)
    good = [_near(i, base=base) for i in (2, 3, 4)]
    stranger = _emb(42)
    kept = drop_outliers([*good, stranger])
    assert kept == good  # order preserved, stranger gone
    # A coherent sitting survives untouched.
    assert drop_outliers(good) == good


def test_verify_enrollment_accepts_a_profile_that_finds_its_resident(tmp_path: Path):
    db = _seed_db(tmp_path)
    insert_embedding(db, "max", _emb(42), sample_count=1, enrolled_via="test")
    base = _emb(1)
    samples = [_near(i, base=base) for i in (2, 3, 4)]
    check = verify_enrollment(
        samples,
        average_embeddings(samples),
        uid="lena",
        candidates=list_embeddings(db),
        threshold=0.55,
        collision_threshold=0.65,
    )
    assert check.ok is True
    assert check.reason == ""
    assert check.min_score > 0.55


def test_verify_enrollment_flags_a_profile_that_does_not_carry():
    """No candidate clears the threshold — the household fallback a real turn
    would take. That's "collect more samples", not success."""
    samples = [_emb(1), _emb(2), _emb(3)]  # unrelated directions
    check = verify_enrollment(
        samples,
        average_embeddings(samples),
        uid="lena",
        candidates=[],
        threshold=0.9,
        collision_threshold=0.95,
    )
    assert check.ok is False
    assert check.reason == REASON_WEAK


def test_verify_enrollment_flags_a_cross_resident_collision(tmp_path: Path):
    """A sample resolving to a DIFFERENT resident is a privacy failure: enrolling
    would let Solaris hand one resident's data to another. It must never read as
    ok, and the verdict must not carry the other resident's uid."""
    db = _seed_db(tmp_path)
    base = _emb(1)
    samples = [base, _near(2, base=base, spread=0.4)]
    # Max is already enrolled with exactly the first sample's embedding.
    insert_embedding(db, "max", base, sample_count=3, enrolled_via="test")

    check = verify_enrollment(
        samples,
        average_embeddings(samples),
        uid="lena",
        candidates=list_embeddings(db),
        threshold=0.55,
        collision_threshold=0.65,
    )
    assert check.ok is False
    assert check.reason == REASON_COLLISION
    assert "max" not in check.reason


def test_verify_enrollment_reads_the_match_not_the_returned_uid():
    """`resolve_speaker` returns its default_uid on a household fallback. If the
    enrolling resident IS that default, a fallback would look like a hit — so the
    verdict is taken off the match. A profile below threshold stays weak even
    when the uid is the household default."""
    samples = [_emb(1), _emb(2), _emb(3)]
    profile = average_embeddings(samples)
    for uid in ("lena", "michael"):  # michael == the box's DEFAULT_UID
        check = verify_enrollment(
            samples,
            profile,
            uid=uid,
            candidates=[],
            threshold=0.95,
            collision_threshold=0.95,
        )
        assert check.ok is False and check.reason == REASON_WEAK


def _rival_case(tmp_path: Path, cosine: float):
    """A sitting whose averaged profile sits at `cosine` from an already-enrolled
    resident — the knob the collision bar is judged on."""
    db = _seed_db(tmp_path)
    base = _emb(1)
    samples = [_near(i, base=base, spread=0.15) for i in (2, 3, 4)]
    profile = average_embeddings(samples)
    insert_embedding(
        db,
        "max",
        _at_cosine(profile, cosine, seed=9),
        sample_count=3,
        enrolled_via="test",
    )
    return samples, profile, list_embeddings(db)


def test_collision_bar_is_not_the_recognition_threshold(tmp_path: Path):
    """A merely similar-sounding household member must still be able to enrol.
    The profile-vs-profile comparison runs on the stricter collision bar, so a
    rival at 0.60 — above the 0.55 recognition threshold, below the 0.65
    collision bar — is not a collision.

    The second half is the direction check: collapse the two bars back onto 0.55
    and the same enrolment is refused. Higher bar = fewer refusals; inverting it
    would silently disable the protection while still reading as ok."""
    samples, profile, candidates = _rival_case(tmp_path, 0.60)

    separate = verify_enrollment(
        samples,
        profile,
        uid="lena",
        candidates=candidates,
        threshold=0.55,
        collision_threshold=0.65,
    )
    assert separate.ok is True
    assert separate.reason == ""

    collapsed = verify_enrollment(
        samples,
        profile,
        uid="lena",
        candidates=candidates,
        threshold=0.55,
        collision_threshold=0.55,
    )
    assert collapsed.ok is False
    assert collapsed.reason == REASON_COLLISION


def test_collision_bar_still_refuses_a_near_identical_profile(tmp_path: Path):
    """The point of the looser bar is not to stop failing closed: a profile that
    really is the enrolled resident is still refused, because storing it would
    merge two residents into one identity."""
    samples, profile, candidates = _rival_case(tmp_path, 0.85)

    check = verify_enrollment(
        samples,
        profile,
        uid="lena",
        candidates=candidates,
        threshold=0.55,
        collision_threshold=0.65,
    )
    assert check.ok is False
    assert check.reason == REASON_COLLISION
    assert check.min_score > 0.65


def test_insert_rejects_wrong_dim(tmp_path: Path):
    db = _seed_db(tmp_path)
    with pytest.raises(ValueError):
        insert_embedding(db, "alice", b"\x00" * 17, sample_count=1, enrolled_via="test")


def test_list_embeddings_skips_malformed_rows(tmp_path: Path):
    db = _seed_db(tmp_path)
    # Manually shove a malformed row in
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO voice_embeddings (uid, embedding, sample_count, enrolled_via) VALUES (?, ?, ?, ?)",
            ("broken", b"\x00\x00\x00", 1, "test"),
        )
        conn.commit()
    insert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")

    embs = list_embeddings(db)
    assert {e.uid for e in embs} == {"alice"}


def test_list_embeddings_empty_when_db_missing(tmp_path: Path):
    assert list_embeddings(str(tmp_path / "nope.db")) == []
    assert list_uids(str(tmp_path / "nope.db")) == []


def test_delete_embedding_roundtrip(tmp_path: Path):
    db = _seed_db(tmp_path)
    insert_embedding(db, "alice", _emb(1), sample_count=1, enrolled_via="test")
    assert delete_embedding(db, "alice") is True
    assert delete_embedding(db, "alice") is False  # idempotent second call
    assert list_uids(db) == []


def test_delete_embedding_removes_every_row_of_that_resident(tmp_path: Path):
    """Un-enrolling is a privacy promise: with several fingerprints per
    resident, leaving one behind would keep recognising someone who asked to
    be forgotten. Other residents must survive untouched."""
    db = _seed_db(tmp_path)
    insert_embedding(db, "anna", _emb(1), sample_count=3, enrolled_via="voice")
    insert_embedding(db, "anna", _emb(2), sample_count=3, enrolled_via="voice")
    insert_embedding(db, "ben", _emb(3), sample_count=3, enrolled_via="voice")

    assert delete_embedding(db, "anna") is True
    assert [row.uid for row in list_embeddings(db)] == ["ben"]
    assert delete_embedding(db, "anna") is False


def test_list_uids_names_each_resident_once(tmp_path: Path):
    """Admin listings show residents, not rows."""
    db = _seed_db(tmp_path)
    insert_embedding(db, "anna", _emb(1), sample_count=3, enrolled_via="voice")
    insert_embedding(db, "anna", _emb(2), sample_count=3, enrolled_via="http")
    insert_embedding(db, "ben", _emb(3), sample_count=3, enrolled_via="voice")
    assert list_uids(db) == ["anna", "ben"]


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
    insert_embedding(db, "alice", alice, sample_count=1, enrolled_via="test")

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

    resolved = await h._resolve_speaker()
    assert resolved == handler.SpeakerResolution("alice", attributed=True, matched=True)

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
    insert_embedding(db, "alice", _emb(7), sample_count=1, enrolled_via="test")

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

    assert await h._resolve_speaker() == handler.SpeakerResolution(
        handler.GUEST_UID, attributed=True, matched=False
    )


async def test_resolve_uid_ambiguous_speaker_routes_to_guest(tmp_path, monkeypatch):
    """Two residents fit the turn almost equally well. The best one clears the
    threshold, so the pre-#1084 rule would have attributed the turn — and a
    wrong attribution hands one resident's data to another. The margin refuses
    it, and the refusal must land on `guest`, never on the household default."""
    import gatekeeper.handler as handler
    from wyoming.audio import AudioChunk, AudioStart

    db = _seed_db(tmp_path)
    turn = _emb(5)
    insert_embedding(
        db,
        "anna",
        _at_cosine(turn, 0.62, seed=51),
        sample_count=3,
        enrolled_via="voice",
    )
    insert_embedding(
        db, "ben", _at_cosine(turn, 0.60, seed=52), sample_count=3, enrolled_via="voice"
    )

    monkeypatch.setattr(
        handler,
        "settings",
        dataclasses.replace(
            handler.settings,
            speaker_id_enabled=True,
            default_uid="household",
            speaker_id_threshold=0.55,
            speaker_match_margin=0.1,
            solaris_db_path=db,
        ),
    )

    class _StubExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return turn

    monkeypatch.setattr(handler, "get_extractor", lambda: _StubExtractor())

    h = handler.GatekeeperHandler(None, None, object())
    h._audio_start = AudioStart(rate=16000, width=2, channels=1)
    h._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 16000)
    ]

    assert await h._resolve_speaker() == handler.SpeakerResolution(
        handler.GUEST_UID, attributed=True, matched=False
    )
    # A refused turn is not a sighting — nobody may be stamped as seen.
    with sqlite3.connect(db) as conn:
        stamps = conn.execute("SELECT last_seen_at FROM voice_embeddings").fetchall()
    assert stamps == [(None,), (None,)]


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
    assert await h._resolve_speaker() == handler.SpeakerResolution(
        "household", attributed=False
    )


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
    assert await h._resolve_speaker() == handler.SpeakerResolution(
        "household", attributed=False
    )


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
    assert await h._resolve_speaker() == handler.SpeakerResolution(
        "household", attributed=False
    )


# -- #1146: the stash is the privacy gate's evidence, so it must fail closed --


def _add_stash_table(db: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE voice_uid_stash (
              transcript TEXT PRIMARY KEY,
              uid        TEXT NOT NULL,
              matched    INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def _facade_speaker_matched(db: str, transcript: str) -> bool:
    """The engine facade's verdict on this turn, as facade.py computes it: the
    row's explicit `matched` claim, and nothing else — not the row's existence,
    not the uid it carries (#1152). Anything else reads as "not matched".

    Mirrored here rather than imported because CI installs the two packages in
    separate jobs; the engine half of the contract is asserted in
    solaris-chat/tests/test_facade.py.
    """
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT matched FROM voice_uid_stash WHERE transcript = ?", (transcript,)
        ).fetchone()
    return bool(row) and row[0] == 1


def _handler_for(monkeypatch, db: str, *, extractor, **overrides):
    import gatekeeper.handler as handler
    from unittest.mock import AsyncMock
    from wyoming.audio import AudioChunk, AudioStart

    fields = {
        "speaker_id_enabled": True,
        "default_uid": "household",
        "speaker_id_threshold": 0.5,
        "solaris_db_path": db,
        **overrides,
    }
    monkeypatch.setattr(
        handler, "settings", dataclasses.replace(handler.settings, **fields)
    )
    monkeypatch.setattr(handler, "get_extractor", lambda: extractor)
    h = handler.GatekeeperHandler(None, None, object())
    h.write_event = AsyncMock()
    h._audio_start = AudioStart(rate=16000, width=2, channels=1)
    h._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 16000)
    ]
    return h


async def test_inert_speaker_id_publishes_no_match(tmp_path, monkeypatch):
    """SOLARIS_SPEAKER_ID_ENABLED=true on the base image (no SpeechBrain), so
    get_extractor() is None and nothing is ever embedded. The turn still has to
    work — but it must not leave a stash row, because the facade would read one
    as "speaker-ID attributed this utterance" and unlock every PERSONAL tool
    for whoever happened to speak."""
    from unittest.mock import AsyncMock

    db = _seed_db(tmp_path)
    _add_stash_table(db)
    insert_embedding(db, "alice", _emb(7), sample_count=1, enrolled_via="test")

    h = _handler_for(monkeypatch, db, extractor=None)
    h._transcribe = AsyncMock(return_value="wie ist mein tag")
    await h._process_stt_provider()

    # HA still gets its transcript back, and the turn runs as household...
    assert h.write_event.await_count == 1
    # ...but nothing claims a speaker was recognised.
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0] == 0
    assert _facade_speaker_matched(db, "wie ist mein tag") is False


async def test_extract_error_publishes_no_match(tmp_path, monkeypatch):
    """The ML image is in place and the extractor blows up mid-turn. Degrading
    to the household uid is fine; presenting that fallback to the facade as a
    recognised resident is the fail-open."""
    from unittest.mock import AsyncMock

    db = _seed_db(tmp_path)
    _add_stash_table(db)
    insert_embedding(db, "alice", _emb(7), sample_count=1, enrolled_via="test")

    class _BrokenExtractor:
        def extract(self, pcm, *, rate, width, channels):
            raise RuntimeError("model not loaded")

    h = _handler_for(monkeypatch, db, extractor=_BrokenExtractor())
    h._transcribe = AsyncMock(return_value="wie ist mein tag")
    await h._process_stt_provider()

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0] == 0
    assert _facade_speaker_matched(db, "wie ist mein tag") is False


async def test_satellite_match_is_published_to_the_facade(tmp_path, monkeypatch):
    """A wyoming-satellite turn (conversation mode) with a genuine, confident
    match. The uid rides the facade POST as `user`, but that only routes the
    conversation — the visibility gate reads the stash, so an unpublished match
    locks PERSONAL out of satellite hardware forever."""
    from unittest.mock import AsyncMock

    db = _seed_db(tmp_path)
    _add_stash_table(db)
    alice = _emb(7)
    insert_embedding(db, "alice", alice, sample_count=1, enrolled_via="test")

    class _StubExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return alice

    h = _handler_for(monkeypatch, db, extractor=_StubExtractor())
    h._transcribe = AsyncMock(return_value="lies mir meine notizen vor")
    h._resolve_location = AsyncMock(return_value=None)
    h._solaris.converse = AsyncMock(return_value="Klar.")
    h._synthesize_and_stream = AsyncMock()

    await h._process_pipeline()

    # The satellite turn ran as before, attributed to alice...
    assert h._solaris.converse.await_args.kwargs["uid"] == "alice"
    # ...and the match reached the facade as an explicit claim, which unlocks
    # PERSONAL.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT uid, matched FROM voice_uid_stash WHERE transcript = ?",
            ("lies mir meine notizen vor",),
        ).fetchone()
    assert row == ("alice", 1)
    assert _facade_speaker_matched(db, "lies mir meine notizen vor") is True


async def test_satellite_unknown_speaker_is_published_as_guest_not_a_match(
    tmp_path, monkeypatch
):
    """Speaker-ID ran and refused: an unknown voice still has to reach the guest
    profile, so the row is written — with `matched=0`, i.e. carrying no
    recognition claim at all. The uid is routing; the claim is the flag."""
    from unittest.mock import AsyncMock

    db = _seed_db(tmp_path)
    _add_stash_table(db)
    insert_embedding(db, "alice", _emb(7), sample_count=1, enrolled_via="test")

    class _StrangerExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return _emb(99)

    h = _handler_for(
        monkeypatch, db, extractor=_StrangerExtractor(), speaker_id_threshold=0.99
    )
    h._transcribe = AsyncMock(return_value="wer bin ich")
    h._resolve_location = AsyncMock(return_value=None)
    h._solaris.converse = AsyncMock(return_value="Klar.")
    h._synthesize_and_stream = AsyncMock()

    await h._process_pipeline()

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT uid, matched FROM voice_uid_stash WHERE transcript = ?",
            ("wer bin ich",),
        ).fetchone()
    assert row == ("guest", 0)
    assert _facade_speaker_matched(db, "wer bin ich") is False


async def test_a_renamed_unknown_speaker_sentinel_still_publishes_no_match(
    tmp_path, monkeypatch
):
    """#1152, the drift the design has to survive: someone changes the
    unknown-speaker sentinel from `guest` to anything else. Under the old
    contract that alone turned an unknown voice into a recognised resident for
    the facade. Now the sentinel is only a routing label — the row still
    carries no match claim, so PERSONAL stays shut whatever the uid says."""
    import gatekeeper.handler as handler
    from unittest.mock import AsyncMock

    db = _seed_db(tmp_path)
    _add_stash_table(db)
    insert_embedding(db, "alice", _emb(7), sample_count=1, enrolled_via="test")
    monkeypatch.setattr(handler, "GUEST_UID", "unknown")

    class _StrangerExtractor:
        def extract(self, pcm, *, rate, width, channels):
            return _emb(99)

    h = _handler_for(
        monkeypatch, db, extractor=_StrangerExtractor(), speaker_id_threshold=0.99
    )
    h._transcribe = AsyncMock(return_value="wer bin ich")
    h._resolve_location = AsyncMock(return_value=None)
    h._solaris.converse = AsyncMock(return_value="Klar.")
    h._synthesize_and_stream = AsyncMock()

    await h._process_pipeline()

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT uid, matched FROM voice_uid_stash WHERE transcript = ?",
            ("wer bin ich",),
        ).fetchone()
    assert row == ("unknown", 0)
    assert _facade_speaker_matched(db, "wer bin ich") is False


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


def test_advertised_languages_follow_the_system_language(monkeypatch):
    """Satellites were told "de" no matter what the pipeline actually
    transcribes. The advertised set now follows SOLARIS_LANGUAGE (#1057)."""
    from gatekeeper.__main__ import _advertised_languages
    from gatekeeper.config import system_language_from_env

    monkeypatch.delenv("SOLARIS_LANGUAGE", raising=False)
    assert system_language_from_env() == "de"
    assert _advertised_languages() == ["de", "en"]

    monkeypatch.setenv("SOLARIS_LANGUAGE", "EN")
    assert system_language_from_env() == "en"
    assert _advertised_languages() == ["en"]

    monkeypatch.setenv("SOLARIS_LANGUAGE", "fr")
    assert _advertised_languages() == ["fr", "en"]

    monkeypatch.setenv("SOLARIS_LANGUAGE", "   ")
    assert system_language_from_env() == "de"
