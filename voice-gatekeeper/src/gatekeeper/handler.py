"""Wyoming event handler for the gatekeeper.

One handler instance per inbound connection. The Phase-0 contract:

  Client → AudioStart, AudioChunk*, AudioStop
  Gatekeeper:
    1. Stream the buffered audio to whisper, await Transcript
    2. POST transcript to the Solaris Engine with (uid, endpoint, trace_id)
    3. Send response text to piper, stream the resulting AudioChunks back
       to the original client

The connection closes after one pipeline turn (Phase 0 is half-duplex per
turn, like HA's voice pipeline). Multi-turn / streaming is a Phase 4 topic.

STT-provider mode (#350): when HA's Assist pipeline uses the gatekeeper as
its Wyoming *STT* engine, the client opens with a `Transcribe` event before
the audio and expects a `Transcript` back — HA, not the gatekeeper, runs the
conversation step. In that mode the gatekeeper transcribes + resolves the
speaking resident, returns the `Transcript` to HA, and stashes
`{transcript -> uid}` for the engine facade to read on the following
`conversation.solaris` turn — it does NOT POST to the facade or synthesize TTS.
The wyoming-satellite turn above (no `Transcribe`) is unchanged.
"""

from __future__ import annotations

import asyncio
import uuid

from gatekeeper.logging import log
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler

from .config import settings
from .embeddings_store import list_embeddings, touch_last_seen, upsert_embedding
from .enroll_stash import (
    add_embedding,
    capture_lock,
    claim_active_request,
    finish_request,
    increment_collected,
    take_embeddings,
)
from .solaris import SolarisClient
from .rooms_store import get_room
from .speaker import average_embeddings, get_extractor, resolve_speaker
from .tts import synthesize_to_writer
from .uid_stash import stash_uid

# The uid an unknown (attempted-but-unmatched) speaker is attributed to; the
# engine facade routes this to the ephemeral guest profile (#351, #353).
GUEST_UID = "guest"


def client_id_from_peername(peer: object) -> str | None:
    """Stable per-connection client id from a socket peername.

    Wyoming's AsyncEventHandler exposes no client identity, so the
    originating satellite is keyed by its socket peer host. TCP peernames
    are (host, port); a UNIX socket yields a str path. Returns None when
    the peer is unavailable so callers fall back to 'unknown'.
    """
    if isinstance(peer, (tuple, list)):
        host = peer[0] if peer else None
        return str(host) if host else None
    if isinstance(peer, str) and peer:
        return peer
    return None


class GatekeeperHandler(AsyncEventHandler):
    """One connection = one pipeline turn."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        info: Info | None = None,
        solaris: SolarisClient | None = None,
    ):
        super().__init__(reader, writer)
        self._info = info
        self.trace_id = str(uuid.uuid4())
        self.client_id = self._resolve_client_id()
        self._audio_start: AudioStart | None = None
        self._audio_buffer: list[AudioChunk] = []
        # Set when the client opens with a Transcribe event — HA's STT client
        # does, a wyoming-satellite doesn't. Selects STT-provider mode (#350).
        self._stt_mode = False
        self._solaris = solaris or SolarisClient(
            settings.engine_url, settings.engine_token
        )
        log.info(
            "gatekeeper.session.open",
            trace_id=self.trace_id,
            client_id=self.client_id,
        )

    def _resolve_client_id(self) -> str | None:
        try:
            peer = self.writer.get_extra_info("peername")
        except Exception:  # noqa: BLE001 — peer info is best-effort
            return None
        return client_id_from_peername(peer)

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            # Satellites send Describe to discover the ASR/TTS capabilities
            # advertised at startup; answer with the Info passed at construction.
            if self._info is not None:
                await self.write_event(self._info.event())
            return True

        if Transcribe.is_type(event.type):
            # HA's Assist pipeline opens an STT request with Transcribe; a
            # wyoming-satellite never sends it. This is the discriminator that
            # puts us in STT-provider mode (#350) for the rest of the turn.
            self._stt_mode = True
            log.info("gatekeeper.stt_provider.transcribe", trace_id=self.trace_id)
            return True

        if AudioStart.is_type(event.type):
            self._audio_start = AudioStart.from_event(event)
            self._audio_buffer = []
            log.info(
                "gatekeeper.audio.start",
                trace_id=self.trace_id,
                rate=self._audio_start.rate,
                width=self._audio_start.width,
                channels=self._audio_start.channels,
            )
            return True

        if AudioChunk.is_type(event.type):
            self._audio_buffer.append(AudioChunk.from_event(event))
            return True

        if AudioStop.is_type(event.type):
            log.info(
                "gatekeeper.audio.stop",
                trace_id=self.trace_id,
                chunks=len(self._audio_buffer),
                stt_mode=self._stt_mode,
            )
            if self._stt_mode:
                await self._process_stt_provider()
            else:
                await self._process_pipeline()
            return False

        # Unknown event types are dropped silently; debug-mode shows them.
        log.debug("gatekeeper.event.unhandled", trace_id=self.trace_id, type=event.type)
        return True

    async def _process_pipeline(self) -> None:
        if not self._audio_buffer or self._audio_start is None:
            log.warn("gatekeeper.audio.empty", trace_id=self.trace_id)
            return

        try:
            transcript = await self._transcribe()
        except Exception as exc:  # noqa: BLE001 — error logged below
            log.error("gatekeeper.stt.error", trace_id=self.trace_id, error=str(exc))
            return

        if not transcript:
            log.warn("gatekeeper.transcript.empty", trace_id=self.trace_id)
            return
        log.info("gatekeeper.transcript", trace_id=self.trace_id)

        uid = await self._resolve_uid()
        endpoint = f"voice-pe:{self.client_id or 'unknown'}"
        location = await self._resolve_location()
        response = await self._solaris.converse(
            text=transcript,
            uid=uid,
            endpoint=endpoint,
            location=location,
            trace_id=self.trace_id,
        )
        if not response:
            log.warn("gatekeeper.solaris.empty", trace_id=self.trace_id)
            return
        log.info("gatekeeper.response", trace_id=self.trace_id, length=len(response))

        try:
            await self._synthesize_and_stream(response)
        except Exception as exc:  # noqa: BLE001
            log.error("gatekeeper.tts.error", trace_id=self.trace_id, error=str(exc))
            return

        log.info("gatekeeper.session.close", trace_id=self.trace_id)

    async def _process_stt_provider(self) -> None:
        """STT-provider mode (#350): transcribe + resolve the speaking
        resident, return a Transcript to HA so its Assist pipeline continues
        to conversation.solaris as normal, and stash {transcript -> uid} for the
        engine facade to read on that following turn. No facade POST, no TTS —
        HA owns the conversation + the spoken response."""
        if not self._audio_buffer or self._audio_start is None:
            log.warn("gatekeeper.audio.empty", trace_id=self.trace_id)
            await self.write_event(Transcript(text="").event())
            return

        try:
            transcript = await self._transcribe()
        except Exception as exc:  # noqa: BLE001 — error logged below
            log.error("gatekeeper.stt.error", trace_id=self.trace_id, error=str(exc))
            await self.write_event(Transcript(text="").event())
            return

        log.info("gatekeeper.transcript", trace_id=self.trace_id)
        if transcript:
            uid = await self._resolve_uid()
            await asyncio.to_thread(
                stash_uid, settings.solaris_db_path, transcript, uid
            )
            log.info("gatekeeper.stt_provider.stash", trace_id=self.trace_id, uid=uid)
            await self._capture_enrollment()

        await self.write_event(Transcript(text=transcript).event())

    async def _capture_enrollment(self) -> None:
        """Reverse enroll-stash (#376): when the engine has opened an enrol
        request, capture THIS turn's PCM as one sample for the candidate uid and
        embed it in-process. Once the target sample count is reached, average the
        embeddings, upsert the resident's `voice_embeddings` row, and write the
        result back. No-op when speaker-ID is off (no extractor → the engine side
        times the request out honestly) or no request is active."""
        extractor = get_extractor()
        if extractor is None or self._audio_start is None or not self._audio_buffer:
            return
        request = await asyncio.to_thread(
            claim_active_request, settings.solaris_db_path
        )
        if request is None:
            return

        pcm = b"".join(c.audio for c in self._audio_buffer)
        try:
            embedding = await asyncio.to_thread(
                extractor.extract,
                pcm,
                rate=self._audio_start.rate,
                width=self._audio_start.width,
                channels=self._audio_start.channels,
            )
        except Exception as exc:  # noqa: BLE001 — extraction errors degrade gracefully
            log.warn(
                "gatekeeper.enroll.extract_error",
                trace_id=self.trace_id,
                error=str(exc),
            )
            return
        if embedding is None:
            # Too short / silence — don't burn a sample slot on it.
            log.info("gatekeeper.enroll.sample_skipped", trace_id=self.trace_id)
            return

        async with capture_lock(request.uid):
            add_embedding(request.uid, embedding)
            collected = await asyncio.to_thread(
                increment_collected, settings.solaris_db_path, request.uid
            )
            log.info(
                "gatekeeper.enroll.captured",
                trace_id=self.trace_id,
                collected=collected,
                target=request.target_samples,
            )
            if collected < request.target_samples:
                return
            embeddings = take_embeddings(request.uid)

        if not embeddings:
            # A concurrent same-uid turn already consumed the buffer and enrolled;
            # this serialised loser has nothing to average — no-op, not a crash.
            return
        try:
            averaged = await asyncio.to_thread(average_embeddings, embeddings)
            await asyncio.to_thread(
                upsert_embedding,
                settings.solaris_db_path,
                request.uid,
                averaged,
                sample_count=len(embeddings),
                enrolled_via="voice",
            )
        except Exception as exc:  # noqa: BLE001 — enrol failure → honest result
            await asyncio.to_thread(
                finish_request,
                settings.solaris_db_path,
                request.uid,
                ok=False,
                result=str(exc),
            )
            log.error(
                "gatekeeper.enroll.failed", trace_id=self.trace_id, error=str(exc)
            )
            return
        await asyncio.to_thread(
            finish_request,
            settings.solaris_db_path,
            request.uid,
            ok=True,
            result=str(len(embeddings)),
        )
        log.info(
            "gatekeeper.enroll.ok", trace_id=self.trace_id, samples=len(embeddings)
        )

    async def _resolve_uid(self) -> str:
        """Phase 2 speaker resolution. Falls back to default_uid on any
        gap (feature disabled, no enrolments, model not loaded, empty
        buffer, embedding extraction failure). The resolver itself is
        in `speaker.py`; this method orchestrates the pieces and keeps
        the conversation pipeline working when the ML path is absent.

        An attempted-but-unmatched speaker is distinct from those gaps:
        speaker-ID ran, embedded the audio, compared against enrolments,
        and no one cleared the threshold (a real non-match). That returns
        the `guest` sentinel so the facade routes the turn to the guest
        profile (#351); every other gap stays `default_uid` so the
        household hot path is unchanged."""
        if not settings.speaker_id_enabled:
            return settings.default_uid
        extractor = get_extractor()
        if extractor is None or self._audio_start is None or not self._audio_buffer:
            return settings.default_uid
        pcm = b"".join(c.audio for c in self._audio_buffer)
        rate = self._audio_start.rate
        width = self._audio_start.width
        channels = self._audio_start.channels
        try:
            query = await asyncio.to_thread(
                extractor.extract, pcm, rate=rate, width=width, channels=channels
            )
        except Exception as exc:  # noqa: BLE001 — extraction errors degrade gracefully
            log.warn(
                "gatekeeper.speaker.extract_error",
                trace_id=self.trace_id,
                error=str(exc),
            )
            return settings.default_uid
        candidates = await asyncio.to_thread(list_embeddings, settings.solaris_db_path)
        uid, match = resolve_speaker(
            query,
            candidates,
            threshold=settings.speaker_id_threshold,
            default_uid=settings.default_uid,
        )
        if match is not None:
            log.info(
                "gatekeeper.speaker.match",
                trace_id=self.trace_id,
                uid=uid,
                best_uid=match.uid,
                score=round(match.score, 4),
                above_threshold=match.above_threshold,
            )
        # A real attempt that matched no enrolled resident (a candidate existed
        # but fell below threshold) is an unknown speaker, not the household —
        # route it to the guest profile. No-candidate / no-embedding gaps keep
        # `uid == default_uid` and stay household.
        if (
            uid == settings.default_uid
            and match is not None
            and not match.above_threshold
        ):
            return GUEST_UID
        if uid != settings.default_uid:
            await asyncio.to_thread(touch_last_seen, settings.solaris_db_path, uid)
        return uid

    async def _resolve_location(self) -> str | None:
        """Room of the originating satellite, or None when unknown. The engine
        uses it to resolve room-dependent commands; absence is what triggers
        the spoken room-enrolment prompt (see #94)."""
        if not self.client_id:
            return None
        try:
            return await asyncio.to_thread(
                get_room, settings.solaris_db_path, self.client_id
            )
        except Exception:  # noqa: BLE001 — room lookup is best-effort
            return None

    async def _transcribe(self) -> str:
        assert self._audio_start is not None
        async with AsyncClient.from_uri(settings.whisper_uri) as client:
            await client.write_event(Transcribe(language="de").event())
            await client.write_event(self._audio_start.event())
            for chunk in self._audio_buffer:
                await client.write_event(chunk.event())
            await client.write_event(AudioStop().event())
            while True:
                evt = await client.read_event()
                if evt is None:
                    return ""
                if Transcript.is_type(evt.type):
                    return Transcript.from_event(evt).text

    async def _synthesize_and_stream(self, text: str) -> None:
        await synthesize_to_writer(settings.piper_uri, text, self.write_event)
