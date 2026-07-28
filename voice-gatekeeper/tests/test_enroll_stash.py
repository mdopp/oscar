"""Tests for the reverse enroll-stash (#376): the gatekeeper-side store and the
handler capture path.

The store mirrors uid_stash; the handler, when an enroll request is active and
speaker-ID is on, captures each onboarding turn's PCM as a sample and after N
enrols the averaged embedding in-process. Tests use a real sqlite db (the three
relevant tables replayed) and a stub extractor that returns a fixed 192-d vector.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import struct
from unittest.mock import AsyncMock

from gatekeeper import enroll_stash
from gatekeeper import handler as handler_mod
from gatekeeper.handler import GatekeeperHandler
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop

_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE enroll_requests (
    uid            TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'pending',
    target_samples INTEGER NOT NULL DEFAULT 3,
    collected      INTEGER NOT NULL DEFAULT 0,
    result         TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE voice_embeddings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    sample_count INTEGER NOT NULL,
    enrolled_via TEXT NOT NULL,
    enrolled_at  TEXT,
    last_seen_at TEXT
);
"""

# A unit-norm-able 192-d float32 vector (768 bytes) the stub extractor returns.
_EMBEDDING = struct.pack("<192f", *([0.5] * 192))


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _StubInfo:
    def event(self):
        return "info-event"


class _StubExtractor:
    def extract(self, pcm, *, rate, width, channels):
        return _EMBEDDING


def _audio_events():
    return [
        AudioStart(rate=16000, width=2, channels=1).event(),
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00").event(),
        AudioStop().event(),
    ]


async def _turn(handler: GatekeeperHandler):
    for ev in [Transcribe().event(), *_audio_events()]:
        await handler.handle_event(ev)


def _new_handler(
    db: str,
    monkeypatch,
    *,
    extractor: object | None,
    threshold: float = 0.55,
    collision_threshold: float = 0.65,
) -> GatekeeperHandler:
    monkeypatch.setattr(
        handler_mod,
        "settings",
        dataclasses.replace(
            handler_mod.settings,
            solaris_db_path=db,
            speaker_id_threshold=threshold,
            speaker_collision_threshold=collision_threshold,
        ),
    )
    monkeypatch.setattr(handler_mod, "get_extractor", lambda: extractor)
    h = GatekeeperHandler(None, None, _StubInfo())
    h.write_event = AsyncMock()
    h._transcribe = AsyncMock(return_value="lena")
    h._resolve_uid = AsyncMock(return_value="guest")
    return h


# --- store ------------------------------------------------------------------


def test_claim_marks_capturing_and_returns_row(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid) VALUES ('lena')")
    conn.commit()
    conn.close()
    req = enroll_stash.claim_active_request(db)
    assert req is not None and req.uid == "lena" and req.target_samples == 3
    # Now marked capturing.
    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT status FROM enroll_requests WHERE uid='lena'"
    ).fetchone()[0]
    conn.close()
    assert status == "capturing"


def test_claim_ignores_stale_request(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO enroll_requests (uid, created_at) VALUES ('lena', datetime('now', ?))",
        (f"-{enroll_stash.ENROLL_TTL_SECONDS + 30} seconds",),
    )
    conn.commit()
    conn.close()
    assert enroll_stash.claim_active_request(db) is None


def test_claim_ignores_terminal_request(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid, status) VALUES ('lena', 'done')")
    conn.commit()
    conn.close()
    assert enroll_stash.claim_active_request(db) is None


def test_claim_missing_db_is_none(tmp_path):
    assert enroll_stash.claim_active_request(str(tmp_path / "absent.db")) is None


def test_increment_and_finish(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid) VALUES ('lena')")
    conn.commit()
    conn.close()
    assert enroll_stash.increment_collected(db, "lena") == 1
    assert enroll_stash.increment_collected(db, "lena") == 2
    enroll_stash.finish_request(db, "lena", ok=True, result="3")
    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT status FROM enroll_requests WHERE uid='lena'"
    ).fetchone()[0]
    conn.close()
    assert status == "done"


def test_accumulator_take_clears(tmp_path):
    enroll_stash.add_embedding("zz", _EMBEDDING)
    assert enroll_stash.add_embedding("zz", _EMBEDDING) == 2
    assert len(enroll_stash.take_embeddings("zz")) == 2
    assert enroll_stash.take_embeddings("zz") == []  # cleared


# --- handler capture path ---------------------------------------------------


async def test_three_turns_enroll_in_process(tmp_path, monkeypatch):
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")  # isolate the process-global buffer
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid, target_samples) VALUES ('lena', 3)")
    conn.commit()
    conn.close()

    for _ in range(3):
        await _turn(_new_handler(db, monkeypatch, extractor=_StubExtractor()))

    conn = sqlite3.connect(db)
    status, collected = conn.execute(
        "SELECT status, collected FROM enroll_requests WHERE uid='lena'"
    ).fetchone()
    emb = conn.execute(
        "SELECT enrolled_via, sample_count FROM voice_embeddings WHERE uid='lena'"
    ).fetchone()
    conn.close()
    assert status == "done"
    assert collected == 3
    assert emb is not None and emb[0] == "voice" and emb[1] == 3


async def test_no_enroll_when_speaker_id_off(tmp_path, monkeypatch):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid) VALUES ('lena')")
    conn.commit()
    conn.close()

    # Extractor None == speaker-ID off: the gatekeeper never picks up the
    # request, so it stays pending for the engine side to time out.
    await _turn(_new_handler(db, monkeypatch, extractor=None))

    conn = sqlite3.connect(db)
    status, collected = conn.execute(
        "SELECT status, collected FROM enroll_requests WHERE uid='lena'"
    ).fetchone()
    n_emb = conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0]
    conn.close()
    assert status == "pending"
    assert collected == 0
    assert n_emb == 0


def test_capture_lock_is_per_uid_and_stable(tmp_path):
    """The serialisation primitive: one shared lock per candidate uid (so the two
    halves of the capture section can't interleave for the same uid), and distinct
    uids get distinct locks (independent enrolments don't block each other)."""
    enroll_stash._capture_locks.clear()
    a1 = enroll_stash.capture_lock("lena")
    a2 = enroll_stash.capture_lock("lena")
    b = enroll_stash.capture_lock("max")
    assert a1 is a2
    assert a1 is not b


async def test_concurrent_same_uid_turns_dont_double_consume(tmp_path, monkeypatch):
    """Two voice turns racing on the SAME enrolment's final sample must not both
    pass the target check, both take_embeddings (loser gets []), and crash on
    average_embeddings([]) (#441).

    Deterministic interleave: a patched take_embeddings holds the FIRST taker until
    the SECOND turn has also reached take, so without the fix both observe a stale
    in-process count, both take, and the loser averages []. With the fix the per-uid
    lock means only one turn is ever inside the add→take section, so the second turn
    never reaches this gate while the first holds it (no deadlock — the gate has a
    bounded fallback), and the empty-take guard makes the loser a clean no-op."""
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")  # isolate the process-global buffer
    enroll_stash._capture_locks.pop("lena", None)
    conn = sqlite3.connect(db)
    # target_samples=1 so each turn alone reaches the target — under the race both
    # turns add to the one buffer before either takes.
    conn.execute("INSERT INTO enroll_requests (uid, target_samples) VALUES ('lena', 1)")
    conn.commit()
    conn.close()

    # Rendezvous between add_embedding and take_embeddings: each turn, after its
    # add + DB increment, waits here so both have added before either takes. Under
    # the race both then see a stale in-process count, both take, loser gets [].
    # Under the fix only one turn ever reaches this point inside the lock, so the
    # barrier times out and the (single) turn proceeds — no deadlock.
    both_added = asyncio.Barrier(2)
    real_to_thread = handler_mod.asyncio.to_thread

    async def rendezvous_to_thread(func, /, *args, **kwargs):
        result = await real_to_thread(func, *args, **kwargs)
        if func is enroll_stash.increment_collected:
            try:
                await asyncio.wait_for(both_added.wait(), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError, asyncio.BrokenBarrierError):
                pass
        return result

    monkeypatch.setattr(handler_mod.asyncio, "to_thread", rendezvous_to_thread)

    h1 = _new_handler(db, monkeypatch, extractor=_StubExtractor())
    h2 = _new_handler(db, monkeypatch, extractor=_StubExtractor())
    await asyncio.gather(_turn(h1), _turn(h2))

    conn = sqlite3.connect(db)
    status, result = conn.execute(
        "SELECT status, result FROM enroll_requests WHERE uid='lena'"
    ).fetchone()
    uids = [r[0] for r in conn.execute("SELECT uid FROM voice_embeddings")]
    conn.close()
    # Enrolment succeeded; no turn crashed with the empty-samples ValueError and
    # the row was not overwritten with a failure result.
    assert status == "done"
    # Both turns completed a capture of their own (target_samples=1), and since
    # #1084 each stores its own fingerprint instead of the second overwriting
    # the first. What must not happen is a row belonging to anyone else.
    assert set(uids) == {"lena"}
    assert "need at least one sample" not in (result or "")
    # The process-global buffer was fully drained — nothing lingers.
    assert enroll_stash.take_embeddings("lena") == []


async def test_drained_buffer_at_target_is_noop_not_crash(tmp_path, monkeypatch):
    """The serialised loser of a concurrent capture reaches the target check but a
    prior turn already drained the buffer — take_embeddings returns []. The guard
    must make this a clean no-op, never average_embeddings([]) → ValueError (#441)."""
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")
    enroll_stash._capture_locks.pop("lena", None)
    conn = sqlite3.connect(db)
    # collected already at target so this turn's increment pushes it past target,
    # and a stubbed empty take simulates the buffer the winning turn already took.
    conn.execute(
        "INSERT INTO enroll_requests (uid, target_samples, collected) "
        "VALUES ('lena', 1, 1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(handler_mod, "take_embeddings", lambda uid: [])

    # Must not raise; the row stays as-is (no failure result written).
    await _turn(_new_handler(db, monkeypatch, extractor=_StubExtractor()))

    conn = sqlite3.connect(db)
    result = conn.execute(
        "SELECT result FROM enroll_requests WHERE uid='lena'"
    ).fetchone()[0]
    n_emb = conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0]
    conn.close()
    assert n_emb == 0
    assert "need at least one sample" not in (result or "")


# --- post-enrolment self-test (#1083) ---------------------------------------


def _emb(seed: int, *, base: bytes | None = None, spread: float = 0.3) -> bytes:
    """A 192-d unit vector: a fresh direction, or a sample from the same sitting
    as `base` (that direction plus `spread` of a unit-length nudge)."""
    import numpy as np

    def unit(v):
        return (v / np.linalg.norm(v)).astype("<f4")

    rng = np.random.default_rng(seed)
    v = unit(rng.standard_normal(192, dtype="<f4"))
    if base is not None:
        v = unit(np.frombuffer(base, dtype="<f4") + spread * v)
    return v.tobytes()


def _at_cosine(base: bytes, target: float, *, seed: int) -> bytes:
    """A 192-d unit vector at exactly `target` cosine similarity to `base`: an
    independent direction, Gram-Schmidt'd off `base`, mixed back in. Lets a test
    place a rival voice precisely between the recognition threshold and the
    stricter collision bar."""
    import numpy as np

    def unit(v):
        return (v / np.linalg.norm(v)).astype("<f4")

    rng = np.random.default_rng(seed)
    b = np.frombuffer(base, dtype="<f4")
    v = rng.standard_normal(192, dtype="<f4")
    orth = unit(v - float(np.dot(v, b)) * b)
    return unit(target * b + np.sqrt(1.0 - target * target) * orth).tobytes()


class _ScriptedExtractor:
    """Returns the next embedding of a script, one per turn."""

    def __init__(self, script: list[bytes]) -> None:
        self._script = list(script)

    def extract(self, pcm, *, rate, width, channels):
        return self._script.pop(0)


def _open_request(db: str, uid: str, target: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO enroll_requests (uid, target_samples) VALUES (?, ?)", (uid, target)
    )
    conn.commit()
    conn.close()


def _request_row(db: str, uid: str):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT status, result FROM enroll_requests WHERE uid = ?", (uid,)
    ).fetchone()
    conn.close()
    return row


async def test_outlier_sample_is_dropped_and_setup_asks_for_another(
    tmp_path, monkeypatch
):
    """One of the three sentences was a cough / a second speaker. Averaging it in
    would enrol a profile that is partly not the resident, so it is dropped — and
    because that leaves too few consistent samples the request stays open for one
    more sentence instead of declaring success (#1083)."""
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")
    base = _emb(1)
    good = [_emb(s, base=base) for s in (2, 3, 4)]
    stranger = _emb(42)
    extractor = _ScriptedExtractor([good[0], good[1], stranger, good[2]])
    _open_request(db, "lena", 3)

    for _ in range(3):
        await _turn(_new_handler(db, monkeypatch, extractor=extractor))

    # Target reached on turn 3, but only two samples agreed — nothing stored yet.
    assert _request_row(db, "lena")[0] == "capturing"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0] == 0
    conn.close()

    # The fourth sentence completes a coherent set of three.
    await _turn(_new_handler(db, monkeypatch, extractor=extractor))

    assert _request_row(db, "lena")[0] == "done"
    conn = sqlite3.connect(db)
    sample_count = conn.execute(
        "SELECT sample_count FROM voice_embeddings WHERE uid='lena'"
    ).fetchone()[0]
    conn.close()
    assert sample_count == 3  # the stranger is not part of the profile
    assert enroll_stash.take_embeddings("lena") == []


async def test_weak_profile_keeps_collecting_then_fails_at_the_cap(
    tmp_path, monkeypatch
):
    """Samples that never resolve to the enrolling resident mean the profile does
    not carry. Setup asks for more sentences rather than claiming success, and at
    MAX_ENROLL_SAMPLES it fails honestly — it never writes an embedding it just
    proved doesn't work."""
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")
    extractor = _ScriptedExtractor([_emb(s) for s in range(10, 20)])  # all unrelated
    _open_request(db, "lena", 3)

    for _ in range(enroll_stash.MAX_ENROLL_SAMPLES - 1):
        await _turn(_new_handler(db, monkeypatch, extractor=extractor, threshold=0.9))
        assert _request_row(db, "lena")[0] == "capturing"

    await _turn(_new_handler(db, monkeypatch, extractor=extractor, threshold=0.9))
    status, result = _request_row(db, "lena")
    assert status == "failed"
    assert result == "self-test: weak profile"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0] == 0
    conn.close()
    enroll_stash.take_embeddings("lena")


async def test_cross_resident_collision_fails_and_never_overwrites(
    tmp_path, monkeypatch
):
    """The new profile resolves to an ALREADY enrolled resident. Storing it would
    let Solaris read Max's notes to Lena, so enrolment fails instead of reporting
    success — and the other resident's row is untouched. The failure reason must
    not name him either: that would leak who else lives here into Lena's
    onboarding."""
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")
    voice = _emb(7)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_embeddings (uid, embedding, sample_count, enrolled_via) "
        "VALUES ('max', ?, 3, 'voice')",
        (voice,),
    )
    conn.commit()
    conn.close()
    _open_request(db, "lena", 3)

    extractor = _ScriptedExtractor(
        [_emb(s, base=voice, spread=0.05) for s in (1, 2, 3)]
    )
    for _ in range(3):
        await _turn(_new_handler(db, monkeypatch, extractor=extractor))

    status, result = _request_row(db, "lena")
    assert status == "failed"
    assert result == "self-test: collision"
    assert "max" not in result
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT uid, embedding FROM voice_embeddings ORDER BY uid"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["max"]  # no lena profile, max unchanged
    assert bytes(rows[0][1]) == voice
    enroll_stash.take_embeddings("lena")


async def test_similar_sounding_second_resident_still_enrols(tmp_path, monkeypatch):
    """Lena sounds like Max — a sibling, a parent and child — but is not Max. Her
    profile lands above the 0.55 recognition threshold against his row and below
    the 0.65 collision bar, and she must be able to enrol: refusing her would
    make a whole household member unusable. This is also the plumbing check that
    the handler feeds the collision comparison its OWN threshold; passing
    speaker_id_threshold there turns this back into a refusal."""
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")
    voice = _emb(7)
    lena = _at_cosine(voice, 0.60, seed=5)
    samples = [_emb(s, base=lena, spread=0.1) for s in (1, 2, 3)]
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_embeddings (uid, embedding, sample_count, enrolled_via) "
        "VALUES ('max', ?, 3, 'voice')",
        (voice,),
    )
    conn.commit()
    conn.close()
    _open_request(db, "lena", 3)

    extractor = _ScriptedExtractor(samples)
    for _ in range(3):
        await _turn(_new_handler(db, monkeypatch, extractor=extractor))

    # The profile really is in the contested band — not trivially far from Max.
    from gatekeeper.speaker import average_embeddings, cosine_match
    from gatekeeper.embeddings_store import VoiceEmbedding

    rival = cosine_match(
        average_embeddings(samples),
        [VoiceEmbedding(uid="max", embedding_bytes=voice, sample_count=3)],
        threshold=0.0,
    )
    assert rival is not None and 0.55 < rival.score < 0.65

    status, _ = _request_row(db, "lena")
    assert status == "done"
    conn = sqlite3.connect(db)
    uids = [r[0] for r in conn.execute("SELECT uid FROM voice_embeddings ORDER BY uid")]
    conn.close()
    assert uids == ["lena", "max"]
    enroll_stash.take_embeddings("lena")


async def test_no_active_request_is_noop(tmp_path, monkeypatch):
    db = _db(tmp_path)  # enroll_requests empty
    await _turn(_new_handler(db, monkeypatch, extractor=_StubExtractor()))
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0]
    conn.close()
    assert n == 0
