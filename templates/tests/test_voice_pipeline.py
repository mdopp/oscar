"""Tests for the Phase-2 HA voice-pipeline wiring in the post-deploy.

wire_voice_pipeline registers wyoming whisper/piper, creates the ollama-
integration conversation agent against the engine facade and builds the
"Solaris" Assist pipeline. The HA REST helpers are monkeypatched with canned
responses; the websocket path is covered by patching HAWebSocket."""

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
    return _load("solaris_pd_voice", TEMPLATES / "solaris" / "post-deploy.py")


def test_flow_create_happy_path(pd, monkeypatch):
    posts = []

    def fake_post(path, token, payload, timeout=30.0):
        posts.append((path, payload))
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        return 200, {"type": "create_entry", "result": {"entry_id": "e1"}}

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    state, result = pd._flow_create("tok", "wyoming", [{"host": "h", "port": 1}])
    assert state == "created"
    assert result == {"entry_id": "e1"}
    assert posts[0][1] == {"handler": "wyoming"}
    assert posts[1][1] == {"host": "h", "port": 1}


def test_flow_create_already_configured_aborts_ok(pd, monkeypatch):
    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        return 200, {"type": "abort", "reason": "already_configured"}

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    state, _ = pd._flow_create("tok", "wyoming", [{"host": "h", "port": 1}])
    assert state == "already"


def test_flow_create_failure_aborts_dangling_flow(pd, monkeypatch):
    deleted = []

    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        return 200, {"flow_id": "f1", "type": "form", "errors": {"base": "x"}}

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(
        pd, "_ha_request_delete", lambda path, token, timeout=10.0: deleted.append(path)
    )
    state, _ = pd._flow_create("tok", "wyoming", [{"host": "h", "port": 1}])
    assert state == "failed"
    assert deleted and "f1" in deleted[0]


def test_conversation_agent_creates_entry_and_subentry(pd, monkeypatch):
    subentry_posts = []

    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        if path == "/api/config/config_entries/flow/f1":
            return 200, {"type": "create_entry", "result": {"entry_id": "oll1"}}
        if path == "/api/config/config_entries/subentries/flow":
            subentry_posts.append(payload)
            return 200, {"flow_id": "s1", "type": "form"}
        if path == "/api/config/config_entries/subentries/flow/s1":
            subentry_posts.append(payload)
            return 200, {"type": "create_entry"}
        raise AssertionError(f"unexpected POST {path}")

    entities = iter(["", "conversation.solaris"])

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(
        pd, "_find_entity", lambda token, prefix, needle="": next(entities, "")
    )
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    entity = pd.ensure_conversation_agent("tok", "8787", "key")
    assert entity == "conversation.solaris"
    assert subentry_posts[0] == {"handler": ["oll1", "conversation"]}
    assert subentry_posts[1]["model"] == pd.ENGINE_MODEL
    assert subentry_posts[1]["name"] == pd.CONVERSATION_AGENT_NAME


def test_conversation_agent_idempotent_when_entity_exists(pd, monkeypatch):
    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        if path == "/api/config/config_entries/flow/f1":
            return 200, {"type": "abort", "reason": "already_configured"}
        raise AssertionError(f"unexpected POST {path}")

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(pd, "_ollama_entry_id", lambda token, url: "oll1")
    monkeypatch.setattr(
        pd, "_find_entity", lambda token, prefix, needle="": "conversation.solaris"
    )
    assert pd.ensure_conversation_agent("tok", "8787", "key") == "conversation.solaris"


class _FakeWS:
    created: list[dict] = []
    preferred: list[str] = []
    updated: list[dict] = []
    pipelines: list[dict] = []

    def __init__(self, token):
        pass

    def cmd(self, payload):
        t = payload["type"]
        if t == "assist_pipeline/pipeline/list":
            return {"pipelines": list(self.pipelines)}
        if t == "assist_pipeline/pipeline/create":
            _FakeWS.created.append(payload)
            return {"id": "p1"}
        if t == "assist_pipeline/pipeline/set_preferred":
            _FakeWS.preferred.append(payload["pipeline_id"])
            return {}
        if t == "assist_pipeline/pipeline/update":
            _FakeWS.updated.append(payload)
            return payload
        raise AssertionError(f"unexpected ws cmd {t}")

    def close(self):
        pass


def test_pipeline_created_and_preferred(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []
    entity_map = {"stt.": "stt.faster_whisper", "tts.": "tts.piper"}
    monkeypatch.setattr(
        pd,
        "_find_entity",
        lambda token, prefix, needle="": entity_map.get(prefix, ""),
    )
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    assigned = []
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: assigned.append(1))
    assert pd.ensure_assist_pipeline("tok", "conversation.solaris") is True
    create = _FakeWS.created[0]
    assert create["name"] == "Solaris"
    assert create["conversation_engine"] == "conversation.solaris"
    assert create["stt_engine"] == "stt.faster_whisper"
    assert create["tts_engine"] == "tts.piper"
    assert create["language"] == "de"
    assert _FakeWS.preferred == ["p1"]
    assert assigned


def test_pipeline_prefers_martin_bridge(pd, monkeypatch):
    # GPU boxes carry the wyoming_openai bridge (servicebay#1815): the
    # pipeline rides tts.openai_streaming with plain `de` + voice `kokoro`.
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return "tts.openai_streaming"
        if prefix == "tts.":
            return "tts.piper"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.solaris") is True
    create = _FakeWS.created[0]
    assert create["tts_engine"] == "tts.openai_streaming"
    assert create["tts_language"] == "de"
    assert create["tts_voice"] == "martin"


def test_pipeline_piper_fallback_keeps_regional_language(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return ""
        if prefix == "tts.":
            return "tts.piper"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.solaris") is True
    create = _FakeWS.created[0]
    assert create["tts_engine"] == "tts.piper"
    assert create["tts_language"] == "de_DE"
    assert create["tts_voice"] is None


def test_existing_pipeline_converges_onto_martin(pd, monkeypatch):
    # A box wired with piper before the Martin units landed gets its
    # pipeline updated to the bridge entity on the next deploy.
    _FakeWS.created, _FakeWS.preferred = [], []
    _FakeWS.updated = []
    _FakeWS.pipelines = [
        {
            "name": "Solaris",
            "id": "p-old",
            "tts_engine": "tts.piper",
            "tts_language": "de_DE",
            "tts_voice": None,
            "conversation_engine": "conversation.solaris",
            "conversation_language": "de",
            "language": "de",
            "stt_engine": "stt.faster_whisper",
            "stt_language": "de",
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    ]

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return "tts.openai_streaming"
        if prefix == "tts.":
            return "tts.piper"
        return ""

    class _WS(_FakeWS):
        def cmd(self, payload):
            if payload["type"] == "assist_pipeline/pipeline/update":
                _FakeWS.updated.append(payload)
                return payload
            return super().cmd(payload)

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _WS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.solaris") is True
    assert _FakeWS.created == []
    upd = _FakeWS.updated[0]
    assert upd["tts_engine"] == "tts.openai_streaming"
    assert upd["tts_language"] == "de"
    assert upd["tts_voice"] == "martin"
    assert _FakeWS.preferred == ["p-old"]


def test_pipeline_idempotent_on_name(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred = [], []
    _FakeWS.pipelines = [{"name": "Solaris", "id": "p-existing"}]
    entity_map = {"stt.": "stt.x", "tts.": "tts.y"}
    monkeypatch.setattr(
        pd,
        "_find_entity",
        lambda token, prefix, needle="": entity_map.get(prefix, ""),
    )
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.solaris") is True
    assert _FakeWS.created == []
    assert _FakeWS.preferred == ["p-existing"]


# -- #350: gatekeeper-as-STT wiring when speaker-ID is on --------------------


def test_pipeline_prefers_gatekeeper_stt_when_speaker_id_on(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt." and needle == "gatekeeper":
            return "stt.solaris_gatekeeper_asr"
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts.":
            return "tts.piper"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    ok = pd.ensure_assist_pipeline(
        "tok", "conversation.solaris", prefer_gatekeeper_stt=True
    )
    assert ok is True
    assert _FakeWS.created[0]["stt_engine"] == "stt.solaris_gatekeeper_asr"


def test_pipeline_stt_unchanged_when_speaker_id_off(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []
    entity_map = {"stt.": "stt.faster_whisper", "tts.": "tts.piper"}
    monkeypatch.setattr(
        pd, "_find_entity", lambda token, prefix, needle="": entity_map.get(prefix, "")
    )
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    pd.ensure_assist_pipeline("tok", "conversation.solaris")  # default: off
    assert _FakeWS.created[0]["stt_engine"] == "stt.faster_whisper"


def test_existing_pipeline_converges_stt_to_gatekeeper(pd, monkeypatch):
    # Toggling speaker-ID on a redeploy moves an existing pipeline's STT from
    # whisper to the gatekeeper.
    _FakeWS.created, _FakeWS.preferred, _FakeWS.updated = [], [], []
    _FakeWS.pipelines = [
        {
            "name": "Solaris",
            "id": "p-old",
            "tts_engine": "tts.piper",
            "tts_language": "de_DE",
            "tts_voice": None,
            "conversation_engine": "conversation.solaris",
            "conversation_language": "de",
            "language": "de",
            "stt_engine": "stt.faster_whisper",
            "stt_language": "de",
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    ]

    def find(token, prefix, needle=""):
        if prefix == "stt." and needle == "gatekeeper":
            return "stt.solaris_gatekeeper_asr"
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return ""
        if prefix == "tts.":
            return "tts.piper"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    ok = pd.ensure_assist_pipeline(
        "tok", "conversation.solaris", prefer_gatekeeper_stt=True
    )
    assert ok is True
    assert _FakeWS.created == []
    assert _FakeWS.updated[0]["stt_engine"] == "stt.solaris_gatekeeper_asr"


def test_wire_registers_gatekeeper_stt_when_speaker_id_on(pd, monkeypatch):
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "_wait_for_port", lambda host, port, timeout_secs=60: True)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    seen = {}
    monkeypatch.setattr(
        pd,
        "ensure_assist_pipeline",
        lambda token, entity, prefer_gatekeeper_stt=False, wake_word_id="": seen.update(
            prefer=prefer_gatekeeper_stt
        ),
    )
    # The flag is read off the gatekeeper container (SB doesn't export the var).
    monkeypatch.setattr(
        pd,
        "gatekeeper_container_env",
        lambda name: "true" if name == "SOLARIS_SPEAKER_ID_ENABLED" else "10700",
    )
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert "gatekeeper" in wired
    assert seen["prefer"] is True


def test_wire_waits_for_gatekeeper_port_before_registering(pd, monkeypatch):
    # #395: the gatekeeper Wyoming STT listener boots after wiring starts, so
    # its entity must not be registered until :10700 answers.
    wired = []
    waited = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)

    def fake_wait(host, port, timeout_secs=60):
        waited.append(port)
        return True

    monkeypatch.setattr(pd, "_wait_for_port", fake_wait)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    seen = {}
    monkeypatch.setattr(
        pd,
        "ensure_assist_pipeline",
        lambda token, entity, prefer_gatekeeper_stt=False, wake_word_id="": seen.update(
            prefer=prefer_gatekeeper_stt
        ),
    )
    monkeypatch.setattr(
        pd,
        "gatekeeper_container_env",
        lambda name: "true" if name == "SOLARIS_SPEAKER_ID_ENABLED" else "10700",
    )
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert waited == [10700]
    assert "gatekeeper" in wired
    assert seen["prefer"] is True


def test_wire_skips_gatekeeper_stt_when_port_never_up(pd, monkeypatch):
    # #395: if :10700 never answers within the deadline, the gatekeeper STT is
    # not registered and the pipeline stays on whisper (prefer_gatekeeper_stt
    # False) rather than pointing at a non-existent STT entity.
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "_wait_for_port", lambda host, port, timeout_secs=60: False)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    seen = {}
    monkeypatch.setattr(
        pd,
        "ensure_assist_pipeline",
        lambda token, entity, prefer_gatekeeper_stt=False, wake_word_id="": seen.update(
            prefer=prefer_gatekeeper_stt
        ),
    )
    monkeypatch.setattr(
        pd,
        "gatekeeper_container_env",
        lambda name: "true" if name == "SOLARIS_SPEAKER_ID_ENABLED" else "10700",
    )
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert "gatekeeper" not in wired
    assert seen["prefer"] is False


def test_wire_no_gatekeeper_stt_when_speaker_id_off(pd, monkeypatch):
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    seen = {}
    monkeypatch.setattr(
        pd,
        "ensure_assist_pipeline",
        lambda token, entity, prefer_gatekeeper_stt=False, wake_word_id="": seen.update(
            prefer=prefer_gatekeeper_stt
        ),
    )
    monkeypatch.setattr(pd, "gatekeeper_container_env", lambda name: "")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert "gatekeeper" not in wired
    assert seen["prefer"] is False


def test_wire_skips_without_token(pd, monkeypatch):
    monkeypatch.setattr(
        pd,
        "ensure_wyoming_entry",
        lambda *a, **k: pytest.fail("must not wire without a token"),
    )
    pd.wire_voice_pipeline("", "8787", "key")


def test_wire_skips_agent_when_engine_down(pd, monkeypatch):
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    # Isolate the optional-bridge probes (:10203 openai/Martin, :10200 piper):
    # unpatched they make a real socket connect, so a host with those ports up
    # wires extra entries and the assert flakes local-vs-CI (#609).
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: False)
    monkeypatch.setattr(
        pd,
        "ensure_conversation_agent",
        lambda *a: pytest.fail("engine down — no agent"),
    )
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert wired == ["whisper"]


# -- #407: custom "Solaris" wake-word install + pipeline wiring ---------------


def test_install_wake_word_model_copies_into_custom_dir(pd, tmp_path):
    src_dir = tmp_path / "solaris" / "wakeword"
    src_dir.mkdir(parents=True)
    (src_dir / "solaris.tflite").write_bytes(b"MODEL")
    custom = tmp_path / "voice" / "custom"
    assert pd.install_wake_word_model(str(tmp_path), str(custom), "solaris") is True
    assert (custom / "solaris.tflite").read_bytes() == b"MODEL"


def test_install_wake_word_model_idempotent(pd, tmp_path):
    src_dir = tmp_path / "solaris" / "wakeword"
    src_dir.mkdir(parents=True)
    (src_dir / "solaris.tflite").write_bytes(b"MODEL")
    custom = tmp_path / "voice" / "custom"
    pd.install_wake_word_model(str(tmp_path), str(custom), "solaris")
    # Second run is a no-op when bytes already match (still True).
    assert pd.install_wake_word_model(str(tmp_path), str(custom), "solaris") is True


def test_install_wake_word_model_absent_is_failsoft(pd, tmp_path):
    # No source model anywhere → False (push-to-talk), no crash, nothing written.
    custom = tmp_path / "voice" / "custom"
    assert pd.install_wake_word_model(str(tmp_path), str(custom), "solaris") is False
    assert not custom.exists()


def test_pipeline_sets_wake_word_when_entity_present(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts.":
            return "tts.piper"
        if prefix == "wake_word.":
            return "wake_word.openwakeword"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    ok = pd.ensure_assist_pipeline(
        "tok", "conversation.solaris", wake_word_id="solaris"
    )
    assert ok is True
    create = _FakeWS.created[0]
    assert create["wake_word_entity"] == "wake_word.openwakeword"
    assert create["wake_word_id"] == "solaris"


def test_pipeline_wake_word_unset_when_entity_missing(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts.":
            return "tts.piper"
        return ""  # no wake_word entity

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    ok = pd.ensure_assist_pipeline(
        "tok", "conversation.solaris", wake_word_id="solaris"
    )
    assert ok is True
    create = _FakeWS.created[0]
    assert create["wake_word_entity"] is None
    assert create["wake_word_id"] is None


def test_existing_pipeline_converges_wake_word(pd, monkeypatch):
    # A model that lands on a later deploy flips wake_word on an existing
    # pipeline that previously had none.
    _FakeWS.created, _FakeWS.preferred, _FakeWS.updated = [], [], []
    _FakeWS.pipelines = [
        {
            "name": "Solaris",
            "id": "p-old",
            "tts_engine": "tts.piper",
            "tts_language": "de_DE",
            "tts_voice": None,
            "conversation_engine": "conversation.solaris",
            "conversation_language": "de",
            "language": "de",
            "stt_engine": "stt.faster_whisper",
            "stt_language": "de",
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    ]

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return ""
        if prefix == "tts.":
            return "tts.piper"
        if prefix == "wake_word.":
            return "wake_word.openwakeword"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    ok = pd.ensure_assist_pipeline(
        "tok", "conversation.solaris", wake_word_id="solaris"
    )
    assert ok is True
    assert _FakeWS.created == []
    upd = _FakeWS.updated[0]
    assert upd["wake_word_entity"] == "wake_word.openwakeword"
    assert upd["wake_word_id"] == "solaris"


def test_existing_pipeline_converges_wake_word_id_when_entity_already_set(
    pd, monkeypatch
):
    # A prior deploy resolved wake_word_entity but never persisted wake_word_id
    # (so the PE still answers to okay-nabu). A redeploy must converge the id
    # even though wake_word_entity already matches.
    _FakeWS.created, _FakeWS.preferred, _FakeWS.updated = [], [], []
    _FakeWS.pipelines = [
        {
            "name": "Solaris",
            "id": "p-old",
            "tts_engine": "tts.piper",
            "tts_language": "de_DE",
            "tts_voice": None,
            "conversation_engine": "conversation.solaris",
            "conversation_language": "de",
            "language": "de",
            "stt_engine": "stt.faster_whisper",
            "stt_language": "de",
            "wake_word_entity": "wake_word.openwakeword",
            "wake_word_id": None,
        }
    ]

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return ""
        if prefix == "tts.":
            return "tts.piper"
        if prefix == "wake_word.":
            return "wake_word.openwakeword"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    ok = pd.ensure_assist_pipeline(
        "tok", "conversation.solaris", wake_word_id="solaris"
    )
    assert ok is True
    assert _FakeWS.created == []
    assert _FakeWS.updated, "redeploy must update the pipeline to persist wake_word_id"
    upd = _FakeWS.updated[0]
    assert upd["wake_word_entity"] == "wake_word.openwakeword"
    assert upd["wake_word_id"] == "solaris"


def test_wire_installs_model_and_passes_wake_word(pd, monkeypatch):
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: None)
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(pd, "gatekeeper_container_env", lambda name: "")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    monkeypatch.setattr(
        pd, "install_wake_word_model", lambda data_dir, custom, mid: True
    )
    seen = {}
    monkeypatch.setattr(
        pd,
        "ensure_assist_pipeline",
        lambda token, entity, prefer_gatekeeper_stt=False, wake_word_id="": seen.update(
            wake=wake_word_id
        ),
    )
    pd.wire_voice_pipeline("tok", "8787", "key", "/mnt/data")
    assert seen["wake"] == "solaris"


def test_wire_no_wake_word_when_model_absent(pd, monkeypatch):
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: None)
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(pd, "gatekeeper_container_env", lambda name: "")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    monkeypatch.setattr(
        pd, "install_wake_word_model", lambda data_dir, custom, mid: False
    )
    seen = {}
    monkeypatch.setattr(
        pd,
        "ensure_assist_pipeline",
        lambda token, entity, prefer_gatekeeper_stt=False, wake_word_id="": seen.update(
            wake=wake_word_id
        ),
    )
    pd.wire_voice_pipeline("tok", "8787", "key", "/mnt/data")
    assert seen["wake"] == ""


def test_wire_registers_openwakeword_integration_when_model_present(pd, monkeypatch):
    # With the model installed and :10400 answering, the openWakeWord wyoming
    # integration must be registered so HA exposes the wake_word entity that
    # ensure_assist_pipeline pins (else the model is inert / push-to-talk).
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: True)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(pd, "gatekeeper_container_env", lambda name: "")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    monkeypatch.setattr(
        pd, "install_wake_word_model", lambda data_dir, custom, mid: True
    )
    monkeypatch.setattr(pd, "ensure_assist_pipeline", lambda *a, **k: None)
    pd.wire_voice_pipeline("tok", "8787", "key", "/mnt/data")
    assert "openwakeword" in wired


def test_wire_skips_openwakeword_integration_without_model(pd, monkeypatch):
    # No model -> no point registering the wake-word integration.
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: True)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: True)
    monkeypatch.setattr(pd, "gatekeeper_container_env", lambda name: "")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd, "ensure_conversation_agent", lambda *a: "conversation.solaris"
    )
    monkeypatch.setattr(
        pd, "install_wake_word_model", lambda data_dir, custom, mid: False
    )
    monkeypatch.setattr(pd, "ensure_assist_pipeline", lambda *a, **k: None)
    pd.wire_voice_pipeline("tok", "8787", "key", "/mnt/data")
    assert "openwakeword" not in wired


# -- #1207/#1208: a deploy log with no red line to explain away ---------------


def test_wire_skips_piper_when_nothing_listens_on_10200(pd, monkeypatch):
    # Solaris ships no piper; registering one that isn't there logged a failed
    # config-flow on every single deploy (#1207).
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: False)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: False)
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert "piper" not in wired


def test_wire_registers_piper_when_10200_answers(pd, monkeypatch):
    # A box that does run a piper elsewhere keeps its TTS entry.
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "_port_open", lambda host, port, timeout=2.0: port == 10200)
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: False)
    monkeypatch.setattr(pd, "gatekeeper_container_env", lambda name: "")
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert wired == ["whisper", "piper"]


def _pe_states():
    """The Voice PE's selects as HA reports them: two take the pipeline name,
    two are acoustics knobs whose options are nothing of the sort (#1208)."""
    return [
        {
            "entity_id": "select.wohnzimmer_voice_assistant_assistent",
            "attributes": {"options": ["preferred", "Solaris"]},
        },
        {
            "entity_id": "select.wohnzimmer_voice_assistant_wake_word",
            "attributes": {"options": ["okay_nabu", "Solaris"]},
        },
        {
            "entity_id": "select.wohnzimmer_voice_assistant_sprechpausen_erkennung",
            "attributes": {"options": ["aggressive", "default", "relaxed"]},
        },
        {
            "entity_id": "select.wohnzimmer_voice_assistant_wake_word_sensitivity",
            "attributes": {"options": ["slightly_sensitive", "moderately_sensitive"]},
        },
    ]


def test_assign_pe_pipeline_skips_selects_without_a_solaris_option(pd, monkeypatch):
    posted = []
    monkeypatch.setattr(
        pd, "_ha_get", lambda path, token, timeout=10.0: (200, _pe_states())
    )
    monkeypatch.setattr(
        pd,
        "_ha_post",
        lambda path, token, payload, timeout=30.0: (posted.append(payload), (200, {}))[
            1
        ],
    )
    pd._assign_pe_pipeline("tok")
    assert [p["entity_id"] for p in posted] == [
        "select.wohnzimmer_voice_assistant_assistent",
        "select.wohnzimmer_voice_assistant_wake_word",
    ]
    assert all(p["option"] == "Solaris" for p in posted)


def test_assign_pe_pipeline_needs_the_options_list_not_the_name(pd, monkeypatch):
    # Name-pattern matching is what produced the HTTP 500s: a select whose
    # entity_id looks right but which offers no Solaris option is left alone.
    states = [
        {
            "entity_id": "select.wohnzimmer_voice_assistant_sprechpausen_erkennung",
            "attributes": {"options": ["aggressive", "default"]},
        }
    ]
    monkeypatch.setattr(pd, "_ha_get", lambda path, token, timeout=10.0: (200, states))
    monkeypatch.setattr(
        pd,
        "_ha_post",
        lambda *a, **k: pytest.fail(
            "must not set a select that cannot take a pipeline"
        ),
    )
    pd._assign_pe_pipeline("tok")


def test_conversation_agent_retries_aborted_flow_start(pd, monkeypatch):
    """HA answers an aborted subentry flow-start with 200 + a flow_id while
    already having removed the flow (#1324) — retry instead of posting the
    second step into a dead flow."""
    starts = []

    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        if path == "/api/config/config_entries/flow/f1":
            return 200, {"type": "abort", "reason": "already_configured"}
        if path == "/api/config/config_entries/subentries/flow":
            starts.append(payload)
            if len(starts) < 3:
                return 200, {
                    "flow_id": f"s{len(starts)}",
                    "type": "abort",
                    "reason": "entry_not_loaded",
                }
            return 200, {"flow_id": "s3", "type": "form", "step_id": "set_options"}
        if path == "/api/config/config_entries/subentries/flow/s3":
            return 200, {"type": "create_entry"}
        raise AssertionError(f"unexpected POST {path}")

    entities = iter(["", "conversation.solaris"])
    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(pd, "_ollama_entry_id", lambda token, url: "oll1")
    monkeypatch.setattr(
        pd, "_find_entity", lambda token, prefix, needle="": next(entities, "")
    )
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    assert pd.ensure_conversation_agent("tok", "8787", "key") == "conversation.solaris"
    assert len(starts) == 3


def test_conversation_agent_reports_abort_reason(pd, monkeypatch):
    """A non-retryable flow-start abort is logged with HA's own reason, not
    the 404 the dead second POST used to produce."""
    logged = []
    posted = []

    def fake_post(path, token, payload, timeout=30.0):
        posted.append(path)
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        if path == "/api/config/config_entries/flow/f1":
            return 200, {"type": "abort", "reason": "already_configured"}
        if path == "/api/config/config_entries/subentries/flow":
            return 200, {"flow_id": "s1", "type": "abort", "reason": "cannot_connect"}
        raise AssertionError(f"unexpected POST {path}")

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(pd, "_ollama_entry_id", lambda token, url: "oll1")
    monkeypatch.setattr(pd, "_find_entity", lambda token, prefix, needle="": "")
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        pd, "jlog", lambda level, tag, message, **kw: logged.append((message, kw))
    )
    assert pd.ensure_conversation_agent("tok", "8787", "key") == ""
    assert "/api/config/config_entries/subentries/flow/s1" not in posted
    assert logged[-1][0] == "conversation subentry not created"
    assert logged[-1][1]["detail"]["reason"] == "cannot_connect"
