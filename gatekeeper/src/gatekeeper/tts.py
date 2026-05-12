"""TTS helper: synthesize a text via Piper, forward audio to a writer.

`synthesize_to_writer` is the one place that talks to Piper. Both the
Wyoming inbound handler (reply-to-the-caller) and the HTTP push endpoint
(push-to-a-named-device) call it with their own `write_event` coroutine.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event
from wyoming.tts import Synthesize, SynthesizeVoice


WriteEvent = Callable[[Event], Awaitable[None]]


async def synthesize_to_writer(
    piper_uri: str, text: str, write_event: WriteEvent, *, voice: str | None = None
) -> int:
    """Render `text` through Piper, forward Audio* events to `write_event`.

    Returns the number of audio chunks forwarded — caller logs that for
    correlation. Raises on connection / read failures so the caller can
    decide how to respond (Wyoming caller drops the turn; HTTP push
    returns 502).
    """
    chunks = 0
    async with AsyncClient.from_uri(piper_uri) as client:
        await client.write_event(
            Synthesize(text=text, voice=SynthesizeVoice(name=voice)).event()
        )
        while True:
            evt = await client.read_event()
            if evt is None:
                return chunks
            if AudioStart.is_type(evt.type):
                await write_event(evt)
            elif AudioChunk.is_type(evt.type):
                await write_event(evt)
                chunks += 1
            elif AudioStop.is_type(evt.type):
                await write_event(evt)
                return chunks
