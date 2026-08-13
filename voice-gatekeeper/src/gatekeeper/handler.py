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
from dataclasses import dataclass

from gatekeeper.logging import log
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler

from .config import settings
from .embeddings_store import insert_embedding, list_embeddings, touch_last_seen
from .enroll_stash import (
    MAX_ENROLL_SAMPLES,
    add_embedding,
    capture_lock,
    claim_active_request,
    finish_request,
    increment_collected,
    restore_embeddings,
    take_embeddings,
)
from .wakeword_stash import (
    claim_active_request as claim_wakeword_request,
    record_sample as record_wakeword_sample,
    sample_path as wakeword_sample_path,
    write_sample as write_wakeword_sample,
)
from .solaris import SolarisClient
from .rooms_store import get_room
from .speaker import (
    REASON_COLLISION,
    average_embeddings,
    drop_outliers,
    get_extractor,
    resolve_speaker,
    verify_enrollment,
)
from .tts import synthesize_to_writer
from .uid_stash import stash_uid

# The uid an unknown (attempted-but-unmatched) speaker is attributed to; the
# engine facade routes this to the ephemeral guest profile (#351, #353).
GUEST_UID = "guest"


@dataclass(frozen=True)
class SpeakerResolution:
    """What speaker-ID concluded about one turn — who, and whether we know it.

    `attributed` is the fail-closed bit for *publishing*: True only when
    speaker-ID actually ran and reached a verdict about this voice — a match
    that cleared threshold and margin, or an explicit non-match routed to
    `guest`. Every gap (feature off, no extractor in this image, extraction
    raised, nobody enrolled) leaves it False, because the uid is then a
    fallback nobody recognised, and no row is written at all (#1146).

    `matched` is the fail-closed bit for *recognition*, and it is the one the
    engine's PERSONAL gate reads: True only for a voice matched to an enrolled
    resident. The unknown-speaker verdict is `uid=guest, matched=False` — it
    routes to the guest profile without ever claiming a recognition, so no uid
    value, today's sentinel or tomorrow's, can stand in for the claim (#1152).
    Both default to the safe answer.
    """

    uid: str
    attributed: bool = False
    matched: bool = False


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

        speaker = await self._resolve_speaker()
        # A satellite turn carries its uid in the facade POST, but `user` only
        # routes the conversation — the visibility gate reads the stash. Publish
        # a genuine match here too, or PERSONAL can never unlock on satellite
        # hardware however confident the match was (#1146).
        await self._stash_speaker(transcript, speaker)
        endpoint = f"voice-pe:{self.client_id or 'unknown'}"
        location = await self._resolve_location()
        response = await self._solaris.converse(
            text=transcript,
            uid=speaker.uid,
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
        engine facade to read on that following turn — when, and only when,
        speaker-ID attributed the turn. No facade POST, no TTS —
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
            speaker = await self._resolve_speaker()
            await self._stash_speaker(transcript, speaker)
            await self._capture_enrollment()
            await self._capture_wakeword_sample()

        await self.write_event(Transcript(text=transcript).event())

    async def _capture_wakeword_sample(self) -> None:
        """Wake-word capture (#1060): while the onboarding wizard has a
        wake-word request open, write THIS turn's PCM as the resident's next
        `.wav` sample and count it. The count is bumped only after the file
        exists, so the wizard's countdown can't run ahead of the audio — with no
        gatekeeper capturing (speaker walked away, engine-only box) nothing
        counts and the wizard says so instead of promising samples. No-op when
        no request is active."""
        if self._audio_start is None or not self._audio_buffer:
            return
        request = await asyncio.to_thread(
            claim_wakeword_request, settings.solaris_db_path
        )
        if request is None:
            return

        pcm = b"".join(c.audio for c in self._audio_buffer)
        path = wakeword_sample_path(
            settings.solaris_db_path, request.uid, request.collected_count + 1
        )
        try:
            await asyncio.to_thread(
                write_wakeword_sample,
                path,
                pcm,
                rate=self._audio_start.rate,
                width=self._audio_start.width,
                channels=self._audio_start.channels,
            )
        except OSError as exc:
            log.error(
                "gatekeeper.wakeword.write_error",
                trace_id=self.trace_id,
                error=str(exc),
            )
            return
        collected = await asyncio.to_thread(
            record_wakeword_sample, settings.solaris_db_path, request.uid
        )
        log.info(
            "gatekeeper.wakeword.captured",
            trace_id=self.trace_id,
            collected=collected,
            target=request.target_count,
        )

    async def _capture_enrollment(self) -> None:
        """Reverse enroll-stash (#376): when the engine has opened an enrol
        request, capture THIS turn's PCM as one sample for the candidate uid and
        embed it in-process. Once the target sample count is reached, average the
        embeddings, add the resident's `voice_embeddings` row, and write the
        result back. No-op when speaker-ID is off (no extractor → the engine side
        times the request out honestly) or no request is active.

        The average is not written on trust (#1083): outlier samples are dropped
        and the profile re-averaged, then every retained sample is resolved the
        way a real turn would be. Only a profile that finds its own resident is
        stored — otherwise the request stays open for another sentence (capped at
        MAX_ENROLL_SAMPLES) or fails, so "erfolgreich gespeichert" is never said
        over a profile that doesn't carry."""
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
            kept = await asyncio.to_thread(drop_outliers, embeddings)
            averaged = await asyncio.to_thread(average_embeddings, kept)
            enrolled = await asyncio.to_thread(
                list_embeddings, settings.solaris_db_path
            )
            check = await asyncio.to_thread(
                verify_enrollment,
                kept,
                averaged,
                uid=request.uid,
                # A re-enrolment compares against the NEW profile, so the
                # resident's own stale row must not be a candidate.
                candidates=[row for row in enrolled if row.uid != request.uid],
                threshold=settings.speaker_id_threshold,
                collision_threshold=settings.speaker_collision_threshold,
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

        # Scores and counts only — naming the resident a sample collided with
        # would put one resident's identity in another's onboarding trail.
        log.info(
            "gatekeeper.enroll.selftest",
            trace_id=self.trace_id,
            kept=len(kept),
            dropped=len(embeddings) - len(kept),
            reason=check.reason or "ok",
            min_score=round(check.min_score, 4),
            threshold=settings.speaker_id_threshold,
            collision_threshold=settings.speaker_collision_threshold,
        )

        if check.reason == REASON_COLLISION:
            # A sample resolving to a different resident is a privacy failure,
            # not a quality one: enrolling would let Solaris read that
            # resident's notes to this one. Terminal, and never stored.
            await asyncio.to_thread(
                finish_request,
                settings.solaris_db_path,
                request.uid,
                ok=False,
                result="self-test: collision",
            )
            log.error("gatekeeper.enroll.collision", trace_id=self.trace_id)
            return

        if not check.ok or len(kept) < request.target_samples:
            if collected >= MAX_ENROLL_SAMPLES:
                await asyncio.to_thread(
                    finish_request,
                    settings.solaris_db_path,
                    request.uid,
                    ok=False,
                    result="self-test: weak profile",
                )
                log.warn("gatekeeper.enroll.weak", trace_id=self.trace_id)
                return
            # The profile doesn't carry yet. Keep the consistent samples and
            # leave the request `capturing` — the wizard then asks for another
            # sentence instead of announcing a success that isn't one.
            restore_embeddings(request.uid, kept)
            log.info(
                "gatekeeper.enroll.more_samples",
                trace_id=self.trace_id,
                collected=collected,
            )
            return

        try:
            await asyncio.to_thread(
                insert_embedding,
                settings.solaris_db_path,
                request.uid,
                averaged,
                sample_count=len(kept),
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
            result=str(len(kept)),
        )
        log.info("gatekeeper.enroll.ok", trace_id=self.trace_id, samples=len(kept))

    def _unattributed(self) -> SpeakerResolution:
        """The household fallback, marked as what it is: no verdict."""
        return SpeakerResolution(settings.default_uid, attributed=False)

    async def _resolve_speaker(self) -> SpeakerResolution:
        """Phase 2 speaker resolution. Falls back to default_uid on any
        gap (feature disabled, no enrolments, model not loaded, empty
        buffer, embedding extraction failure). The resolver itself is
        in `speaker.py`; this method orchestrates the pieces and keeps
        the conversation pipeline working when the ML path is absent.

        Every one of those gaps comes back `attributed=False`, so the caller
        can tell "nobody looked" from "we recognised this person" — the
        distinction the visibility gate turns on.

        An attempted-but-unmatched speaker is distinct from those gaps:
        speaker-ID ran, embedded the audio, compared against enrolments,
        and no one cleared the threshold (a real non-match). That returns
        the `guest` sentinel so the facade routes the turn to the guest
        profile (#351); every other gap stays `default_uid` so the
        household hot path is unchanged."""
        if not settings.speaker_id_enabled:
            return self._unattributed()
        extractor = get_extractor()
        if extractor is None or self._audio_start is None or not self._audio_buffer:
            return self._unattributed()
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
            return self._unattributed()
        candidates = await asyncio.to_thread(list_embeddings, settings.solaris_db_path)
        uid, match = resolve_speaker(
            query,
            candidates,
            threshold=settings.speaker_id_threshold,
            margin=settings.speaker_match_margin,
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
                runner_up=round(match.runner_up_score, 4),
                margin=round(match.margin, 4),
            )
        # No-candidate / no-embedding gaps carry no match at all: speaker-ID
        # reached no verdict about this voice, so it stays an unattributed
        # household turn.
        if match is None:
            return self._unattributed()
        # A real attempt that matched no enrolled resident (a candidate existed,
        # but scored too low or too close to another resident to tell them
        # apart) is an unknown speaker, not the household — route it to the
        # guest profile.
        if not match.accepted(margin=settings.speaker_match_margin):
            return SpeakerResolution(GUEST_UID, attributed=True, matched=False)
        await asyncio.to_thread(touch_last_seen, settings.solaris_db_path, uid)
        return SpeakerResolution(uid, attributed=True, matched=True)

    async def _stash_speaker(self, transcript: str, speaker: SpeakerResolution) -> None:
        """Publish this turn's speaker to the engine facade over the
        transcript-keyed side-channel — and only when speaker-ID reached a
        verdict.

        Two facts go over the wire, not one: `uid` routes the turn, `matched`
        is the recognition claim the facade's PERSONAL gate reads (#1152). The
        household fallback is published as neither — a gap leaves no row at
        all, so an install where speaker-ID is switched on but inert can't
        report every voice in the room as a recognised resident (#1146), and
        the facade's miss path puts the turn back on the household default."""
        if not speaker.attributed:
            log.info(
                "gatekeeper.speaker.unattributed",
                trace_id=self.trace_id,
                uid=speaker.uid,
            )
            return
        await asyncio.to_thread(
            stash_uid,
            settings.solaris_db_path,
            transcript,
            speaker.uid,
            matched=speaker.matched,
        )
        log.info(
            "gatekeeper.speaker.stash",
            trace_id=self.trace_id,
            uid=speaker.uid,
            matched=speaker.matched,
        )

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
