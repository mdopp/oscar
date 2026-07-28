"""The browser/app wake-word recorder (#1081).

Wake-word samples cannot be collected over the Voice PE: the device detects
"Solaris" on-device and consumes it as an activation, so the word never reaches
the server as content. The wizard therefore stops offering that step and points
at the chat surface, which has a microphone and no wake-word gate.

Covered here: the wizard's terminal reply; the upload endpoint's uid scoping (a
resident can only ever write into their OWN set — the uid comes from the SSO
identity, never from the request); format/size rejection with a plain-language
error; and the counter, which is recomputed from the files on disk so a
re-record or a retried upload cannot double-count (#1080).

Also covered: the "train now" action on the finish card (#1088) — enqueueing
needs a session, takes its uid from the identity, is refused while a run is
queued or running, and the status endpoint reports every state including a
failure's reason. No test starts a real run: the queued row IS the whole
engine-side handshake, the hours of GPU belong to the trainer companion.

The card itself (`static/index.html`) is plain HTML/JS with no test harness in
this repo — its audio path is verified in a real browser, not here. What IS
locked down here is the wording and the markup contract: the resident-facing
name ("Wakeword", never "Weckwort"), the honest finish card, and the presence of
the recording indicator + waveform canvases. Whether the curve actually tracks
the microphone is a browser check.
"""

from __future__ import annotations

import io
import os
import sqlite3
import wave

import pytest

from solaris_chat import wakeword_requests_store, wakeword_samples_store
from solaris_chat.engine import enrollment_fsm
from solaris_chat.engine.tools import register as register_tools
from solaris_chat.server import STATIC_DIR, build_app

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _wav(seconds: float = 1.0, rate: int = 16000, channels: int = 1, width: int = 2):
    """A silent WAV in the format the trainer expects — 16 kHz mono int16."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(rate * seconds) * channels * width)
    return buf.getvalue()


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    sqlite3.connect(path).close()
    return path


@pytest.fixture
def app(tmp_path, db):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )


async def _upload(client, uid, index, data, extra=None):
    form = {"index": str(index), **(extra or {})}
    form["file"] = io.BytesIO(data)
    return await client.post(
        "/api/wakeword/samples", data=form, headers={"Remote-User": uid}
    )


# ---- the wizard no longer offers the dead end ------------------------------


def test_wizard_ends_by_pointing_at_the_app_or_browser(db):
    enrollment_fsm.handle_turn(db, "Richte meinen Benutzer ein.")
    enrollment_fsm.handle_turn(db, "ja")
    enrollment_fsm.handle_turn(db, "Alex")
    for i in range(4):
        enrollment_fsm.handle_turn(db, f"Satz {i}")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE enroll_requests SET status = 'done', result = 'ok'")
        conn.commit()

    final = enrollment_fsm.handle_turn(db, "Letzter Satz")

    assert "erfolgreich gespeichert" in final
    assert "Solaris-App oder im Browser" in final
    assert "Wakeword" in final and "Weckwort" not in final
    # A trailing "?" keeps the Voice PE microphone open; there is nothing left
    # to answer, so the dialog must close.
    assert not final.rstrip().endswith("?")
    assert "Aufnahmen von diesem Wort" not in final
    assert enrollment_fsm.get_fsm_state(db, "default") is None
    # Nothing was opened for the gatekeeper to capture into.
    assert wakeword_requests_store.get_request(db, "alex") is None


# ---- authentication & uid scoping -----------------------------------------


async def test_upload_without_a_session_is_rejected(aiohttp_client, app, db):
    client = await aiohttp_client(app)
    r = await client.post("/api/wakeword/samples", data={"index": "1"})
    assert r.status == 401
    assert wakeword_samples_store.list_samples(db, "solaris") == []


async def test_upload_ignores_a_uid_in_the_request(aiohttp_client, app, db):
    """The uid comes from the SSO identity. A `uid` field naming someone else
    must not put a sample into that resident's set."""
    client = await aiohttp_client(app)
    r = await _upload(client, "lena", 1, _wav(), extra={"uid": "mdopp"})
    assert r.status == 200

    assert os.path.exists(wakeword_samples_store.sample_path(db, "lena", 1))
    assert not os.path.exists(wakeword_samples_store.sample_path(db, "mdopp", 1))
    assert wakeword_samples_store.get_sample(db, "sample_lena_1")["resident_uid"] == (
        "lena"
    )
    assert wakeword_samples_store.get_sample(db, "sample_mdopp_1") is None
    assert wakeword_requests_store.get_request(db, "mdopp") is None


async def test_state_and_playback_are_own_samples_only(aiohttp_client, app, db):
    client = await aiohttp_client(app)
    await _upload(client, "lena", 1, _wav())

    mine = await (
        await client.get("/api/wakeword/samples", headers={"Remote-User": "lena"})
    ).json()
    theirs = await (
        await client.get("/api/wakeword/samples", headers={"Remote-User": "mdopp"})
    ).json()
    assert mine["samples"] == [1] and mine["collected"] == 1
    assert theirs["samples"] == [] and theirs["collected"] == 0

    ok = await client.get("/api/wakeword/samples/1", headers={"Remote-User": "lena"})
    assert ok.status == 200 and ok.headers["Content-Type"] == "audio/wav"
    nope = await client.get("/api/wakeword/samples/1", headers={"Remote-User": "mdopp"})
    assert nope.status == 404


# ---- format & size rejection ----------------------------------------------


@pytest.mark.parametrize(
    "data,status",
    [
        (b"not a wav at all", 415),
        (_wav(rate=44100), 415),
        (_wav(channels=2), 415),
        (_wav(width=1), 415),
        (_wav(seconds=0.05), 400),
        (_wav(seconds=6.0), 413),
    ],
)
async def test_malformed_upload_is_rejected_with_a_plain_message(
    aiohttp_client, app, db, data, status
):
    client = await aiohttp_client(app)
    r = await _upload(client, "lena", 1, data)
    assert r.status == status
    body = await r.json()
    assert body["ok"] is False
    assert "Solaris" in body["error"] or "Aufnahme" in body["error"]
    assert not os.path.exists(wakeword_samples_store.sample_path(db, "lena", 1))
    assert wakeword_samples_store.list_samples(db, "solaris") == []


@pytest.mark.parametrize("index", ["0", "11", "abc", "-1"])
async def test_index_outside_the_set_is_rejected(aiohttp_client, app, db, index):
    client = await aiohttp_client(app)
    r = await _upload(client, "lena", index, _wav())
    assert r.status == 400
    assert wakeword_samples_store.list_samples(db, "solaris") == []


async def test_a_non_multipart_body_is_rejected(aiohttp_client, app):
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/wakeword/samples", json={"index": 1}, headers={"Remote-User": "lena"}
    )
    assert r.status == 400
    assert (await r.json())["ok"] is False


# ---- the file, the row and the counter ------------------------------------


async def test_upload_writes_the_file_the_row_and_the_counter_once(
    aiohttp_client, app, db
):
    client = await aiohttp_client(app)
    body = await (await _upload(client, "lena", 1, _wav())).json()

    assert body == {
        "ok": True,
        "index": 1,
        "target": 10,
        "collected": 1,
        "done": False,
    }
    path = wakeword_samples_store.sample_path(db, "lena", 1)
    assert path.endswith(
        os.path.join("wakeword", "user_samples", "lena", "sample_lena_1.wav")
    )
    with wave.open(path) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)

    sample = wakeword_samples_store.get_sample(db, "sample_lena_1")
    assert sample["resident_uid"] == "lena"
    assert sample["intended_phrase"] == "Solaris"
    assert sample["is_valid"] == 1
    assert sample["filename"] == path

    request = wakeword_requests_store.get_request(db, "lena")
    assert request["collected_count"] == 1
    assert request["target_count"] == 10
    # NOT `active`: that is the gatekeeper's capture flag, and an active row
    # reroutes every resident's turn into the enrolment path.
    assert request["status"] == wakeword_requests_store.STATUS_BROWSER
    assert wakeword_requests_store.has_any_active_request(db) is False


async def test_retrying_the_same_index_does_not_double_count(aiohttp_client, app, db):
    client = await aiohttp_client(app)
    await _upload(client, "lena", 1, _wav())
    again = await (await _upload(client, "lena", 1, _wav())).json()

    assert again["collected"] == 1
    assert wakeword_requests_store.get_request(db, "lena")["collected_count"] == 1
    assert len(wakeword_samples_store.list_samples(db, "solaris")) == 1


async def test_re_recording_replaces_the_sample_and_keeps_the_count(
    aiohttp_client, app, db
):
    client = await aiohttp_client(app)
    await _upload(client, "lena", 1, _wav(seconds=1.0))
    await _upload(client, "lena", 2, _wav(seconds=1.0))
    replaced = await (await _upload(client, "lena", 1, _wav(seconds=2.0))).json()

    assert replaced["collected"] == 2
    with wave.open(wakeword_samples_store.sample_path(db, "lena", 1)) as w:
        assert w.getnframes() == 32000
    assert wakeword_requests_store.get_request(db, "lena")["collected_count"] == 2


async def test_the_full_set_reports_done(aiohttp_client, app, db):
    client = await aiohttp_client(app)
    replies = [
        await (await _upload(client, "lena", i, _wav())).json() for i in range(1, 11)
    ]

    assert [r["collected"] for r in replies] == list(range(1, 11))
    assert [r["done"] for r in replies] == [False] * 9 + [True]
    assert wakeword_requests_store.get_request(db, "lena")["status"] == "completed"
    assert len(wakeword_samples_store.list_samples(db, "solaris")) == 10


# ---- what the card says: "Wakeword", and nothing untrue -------------------


def test_the_card_says_wakeword_everywhere_a_resident_can_read_it():
    """ "Weckwort klingt falsch" — the resident-facing name is "Wakeword", in
    German too. The only survivor is the /weckwort command alias, kept so a
    resident who learned the old command does not hit a dead end."""
    resident_text = _HTML.replace(
        'if (cmd === "wakeword" || cmd === "weckwort")', ""
    ).replace('// "weckwort" stays as a silent alias', "")
    assert "Weckwort" not in resident_text
    assert "weckwort" not in resident_text

    assert "<h2>Wakeword „Solaris“</h2>" in _HTML
    assert 'wakeword: "Wakeword „Solaris“"' in _HTML
    assert (
        '["/wakeword", "Das Wakeword „Solaris“ mit deiner Stimme aufnehmen"]' in _HTML
    )
    assert (
        '["wakeword", "/wakeword", "Das Wakeword „Solaris“ mit deiner Stimme aufnehmen"]'
        in _HTML
    )
    # The alias still routes — /weckwort must not become a dead command.
    assert 'if (cmd === "wakeword" || cmd === "weckwort")' in _HTML


def test_the_spoken_replies_say_wakeword_too():
    fsm = enrollment_fsm.__file__
    for path in (fsm, register_tools.__file__):
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        assert "Für das Wakeword „Solaris“" in body
        assert "Weckwort" not in body


def test_the_finish_card_says_the_recordings_are_used_and_who_starts_it():
    """Since #1074 a run folds the resident's own recordings into the positive
    set, so the old "es benutzt deine Aufnahmen bisher nicht" is no longer
    true. What must stay is that nothing starts by itself."""
    raw = _HTML.split('id="ww-done"', 1)[1].split("</div>", 1)[0]
    done = " ".join(raw.split())
    assert "Fertig — alle 10 Aufnahmen sind gespeichert." in done
    assert (
        "Beim Training lernt Solaris deine Aufnahmen mit. Los geht das Training "
        "aber nur, wenn du es unten startest." in done
    )
    assert "benutzt deine Aufnahmen bisher nicht" not in _HTML
    # Never a background promise: a run only ever starts on a tap.
    assert "im Hintergrund" not in raw
    assert "trainiert das Wakeword" not in _HTML


def test_the_training_card_is_honest_about_hours_and_the_manual_flash():
    """Two things a resident must not have to guess: a run costs hours of GPU,
    and a finished run produces a file — flashing it onto the Voice PE is still
    a manual ESPHome step, so the box does not start hearing anything new."""
    raw = _HTML.split('id="ww-train"', 1)[1].split("</section>", 1)[0]
    box = " ".join(raw.split())
    assert "<strong>ein paar Stunden</strong>" in box
    assert "muss die Datei danach noch jemand von Hand auf das Gerät spielen" in box
    assert ">Wakeword jetzt trainieren</button>" in box
    # The state line is text, never colour alone, and it is announced.
    assert 'id="ww-train-state"' in box and 'aria-live="polite"' in box
    for state in ("Eingereiht", "Läuft", "Fertig", "Fehlgeschlagen"):
        assert '"' + state in _HTML


def test_the_card_reads_the_run_state_on_load_and_polls_while_it_runs():
    """A run takes hours: the card must survive a reload and a fresh visit, so
    the state comes from the server on load — not only from the own tap."""
    assert 'fetch("/api/wakeword/training")' in _HTML
    assert 'wwTrainLoad();\n        return fetch("/api/wakeword/samples")' in _HTML
    assert "wwTrainTimer = setInterval(wwTrainLoad, WW_TRAIN_POLL_MS);" in _HTML
    assert "clearInterval(wwTrainTimer); wwTrainTimer = 0;" in _HTML
    # A queued/running run disables the button instead of queueing a second one.
    assert "wwTrainBtn.disabled = wwTrainBusy || wwTrainPending();" in _HTML


# ---- enqueueing a run: only on a tap, only one at a time ------------------


def _queue_table(db: str) -> None:
    """`wakeword_training_runs` as migration 0029 creates it — the engine never
    creates it itself."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS wakeword_training_runs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'queued',"
            " requested_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " started_at TEXT, finished_at TEXT, result TEXT, model_path TEXT)"
        )
        conn.commit()


def _runs(db: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT id, uid, status FROM wakeword_training_runs ORDER BY id"
        ).fetchall()


async def test_enqueue_without_a_session_is_rejected(aiohttp_client, app, db):
    _queue_table(db)
    client = await aiohttp_client(app)
    r = await client.post("/api/wakeword/training")
    assert r.status == 401
    assert _runs(db) == []
    assert (await client.get("/api/wakeword/training")).status == 401


async def test_enqueue_takes_the_uid_from_the_identity_not_the_body(
    aiohttp_client, app, db
):
    _queue_table(db)
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/wakeword/training",
        json={"uid": "mdopp"},
        headers={"Remote-User": "lena"},
    )
    assert r.status == 200
    assert (await r.json())["run"]["status"] == "queued"
    assert _runs(db) == [(1, "lena", "queued")]
    # The capture switch stays untouched: an `active` wakeword_requests row
    # reroutes every resident's turn into the enrolment path (#1082).
    assert wakeword_requests_store.has_any_active_request(db) is False


@pytest.mark.parametrize("pending", ["queued", "running"])
async def test_a_second_run_is_refused_while_one_is_pending(
    aiohttp_client, app, db, pending
):
    _queue_table(db)
    client = await aiohttp_client(app)
    await client.post("/api/wakeword/training", headers={"Remote-User": "lena"})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE wakeword_training_runs SET status = ?", (pending,))
        conn.commit()

    again = await client.post("/api/wakeword/training", headers={"Remote-User": "max"})

    assert again.status == 409
    body = await again.json()
    assert body["ok"] is False
    assert "läuft schon ein Training" in body["error"]
    assert body["run"]["status"] == pending
    # Hours of GPU are not queued twice — not even by another resident.
    assert _runs(db) == [(1, "lena", pending)]


@pytest.mark.parametrize("terminal", ["done", "failed"])
async def test_a_finished_run_does_not_block_the_next_one(
    aiohttp_client, app, db, terminal
):
    _queue_table(db)
    client = await aiohttp_client(app)
    await client.post("/api/wakeword/training", headers={"Remote-User": "lena"})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE wakeword_training_runs SET status = ?", (terminal,))
        conn.commit()

    again = await client.post("/api/wakeword/training", headers={"Remote-User": "lena"})

    assert again.status == 200
    assert _runs(db) == [(1, "lena", terminal), (2, "lena", "queued")]


async def test_enqueue_reports_honestly_when_there_is_no_queue(aiohttp_client, app, db):
    """Without migration 0029 nothing can claim a run. Saying "eingeplant"
    anyway would leave the resident waiting for a model nobody builds."""
    client = await aiohttp_client(app)
    r = await client.post("/api/wakeword/training", headers={"Remote-User": "lena"})
    assert r.status == 503
    body = await r.json()
    assert body["ok"] is False and "nicht einplanen" in body["error"]


# ---- the status endpoint reports what is really true ----------------------


async def test_status_is_empty_before_anything_was_ever_queued(aiohttp_client, app, db):
    _queue_table(db)
    client = await aiohttp_client(app)
    r = await client.get("/api/wakeword/training", headers={"Remote-User": "lena"})
    assert await r.json() == {"ok": True, "run": None}


@pytest.mark.parametrize(
    "status,result",
    [
        ("queued", None),
        ("running", None),
        ("done", "Modell gebaut: solaris-micro.tflite + solaris-micro.json"),
        ("failed", "CalledProcessError: Kein CUDA-Gerät gefunden"),
    ],
)
async def test_status_reports_each_state_and_the_failure_reason(
    aiohttp_client, app, db, status, result
):
    _queue_table(db)
    client = await aiohttp_client(app)
    await client.post("/api/wakeword/training", headers={"Remote-User": "lena"})
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE wakeword_training_runs SET status = ?, result = ?,"
            " started_at = '2026-07-28 08:00:00', finished_at = '2026-07-28 11:30:00'",
            (status, result),
        )
        conn.commit()

    run = (
        await (
            await client.get("/api/wakeword/training", headers={"Remote-User": "lena"})
        ).json()
    )["run"]

    assert run["status"] == status
    assert run["result"] == (result or "")
    # UTC, marked as such: SQLite writes `datetime('now')` without a zone and
    # the browser would render it as local time.
    assert run["started_at"] == "2026-07-28T08:00:00Z"
    assert run["finished_at"] == "2026-07-28T11:30:00Z"
    assert run["requested_at"].endswith("Z")


async def test_the_newest_run_is_the_one_reported(aiohttp_client, app, db):
    """The household trains one model for everyone, so the state a resident
    sees is the box's newest run — including one another resident started."""
    _queue_table(db)
    client = await aiohttp_client(app)
    await client.post("/api/wakeword/training", headers={"Remote-User": "max"})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE wakeword_training_runs SET status = 'failed'")
        conn.commit()
    await client.post("/api/wakeword/training", headers={"Remote-User": "max"})

    r = await client.get("/api/wakeword/training", headers={"Remote-User": "lena"})

    assert (await r.json())["run"]["status"] == "queued"


# ---- what the card shows while and after recording ------------------------


def test_the_recording_window_is_shown_and_named():
    """The button turning red says nothing about when capture started or how
    long is left. A panel with a draining clock, a remaining-seconds readout and
    the clip length in words does."""
    assert 'id="ww-live"' in _HTML and 'id="ww-clock"' in _HTML
    assert 'id="ww-live-left"' in _HTML
    assert "Aufnahme läuft — sag jetzt „Solaris“" in _HTML
    assert "<strong>1,6 Sekunden</strong>" in _HTML
    assert "Jede Aufnahme dauert nur eine Sekunde" not in _HTML
    # Clock and capture run off one start time, so the bar cannot lie about the
    # window; the stop stays on a timer because a background tab gets no frames.
    assert "var t0 = performance.now();\n            wwLiveShow(t0);" in _HTML
    assert "}, wwClipMs);" in _HTML


def test_every_sample_gets_a_waveform_drawn_from_peaks():
    assert 'id="ww-live-wave"' in _HTML
    assert 'cv.className = "ww-wave"' in _HTML
    assert "function wwPeaksFrom(" in _HTML and "function wwDrawWave(" in _HTML
    # Finished samples come from the endpoint and are decoded in the browser…
    assert 'fetch("/api/wakeword/samples/" + index)' in _HTML
    assert "decodeAudioData" in _HTML
    # …once: the peaks are cached per index, and the just-recorded clip seeds the
    # cache straight from the PCM instead of a round trip.
    assert "var wwPeaks = {};" in _HTML
    assert "if (wwPeaks[index]) return Promise.resolve(wwPeaks[index]);" in _HTML
    assert "wwPeaks[index] = wwPeaksFrom(clip.samples, WW_COLS);" in _HTML
    assert "delete wwPeaks[index];" in _HTML


def test_the_curve_is_self_contained_and_labelled():
    """A strict CSP applies to this origin: no library, no external asset. And a
    curve alone means nothing to a non-technical resident, so the two states that
    make a recording unusable are also said in words."""
    assert "<canvas" in _HTML.split('id="ww-live"', 1)[1][:600]
    assert 'cv.setAttribute("aria-label", "Tonkurve von Aufnahme " + i);' in _HTML
    assert '"nichts zu hören"' in _HTML
    assert '"zu laut — etwas weiter weg sprechen"' in _HTML
