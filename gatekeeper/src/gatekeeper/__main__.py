"""Gatekeeper entry point — start the Wyoming server on the configured URI."""

from __future__ import annotations

import asyncio

from oscar_logging import log
from wyoming.info import AsrModel, AsrProgram, Attribution, Info, TtsProgram
from wyoming.server import AsyncServer

from .config import settings
from .handler import GatekeeperHandler


def _info() -> Info:
    """Self-describe so satellites can introspect what we offer.

    Phase 0 advertises the gatekeeper as a combined ASR+TTS pipeline server.
    The underlying models are configured via env vars (Whisper + Piper);
    satellite clients see one logical endpoint here.
    """
    return Info(
        asr=[
            AsrProgram(
                name="oscar-gatekeeper-asr",
                description="OSCAR gatekeeper — ASR via internal Whisper",
                attribution=Attribution(
                    name="OSCAR", url="https://github.com/mdopp/oscar"
                ),
                installed=True,
                models=[
                    AsrModel(
                        name="oscar-gatekeeper",
                        description="Gatekeeper pipeline (Whisper -> HERMES -> Piper)",
                        attribution=Attribution(
                            name="OSCAR", url="https://github.com/mdopp/oscar"
                        ),
                        installed=True,
                        languages=["de", "en"],
                    )
                ],
            )
        ],
        tts=[
            TtsProgram(
                name="oscar-gatekeeper-tts",
                description="OSCAR gatekeeper — TTS via internal Piper",
                attribution=Attribution(
                    name="OSCAR", url="https://github.com/mdopp/oscar"
                ),
                installed=True,
                voices=[],
            )
        ],
    )


async def _serve() -> None:
    server = AsyncServer.from_uri(settings.gatekeeper_uri)
    log.info("gatekeeper.boot", uri=settings.gatekeeper_uri)
    await server.run(lambda r, w: GatekeeperHandler(r, w, _info()))


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
