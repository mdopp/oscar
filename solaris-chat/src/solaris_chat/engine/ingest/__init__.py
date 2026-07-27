"""Ingestion adapters — the OKF write-path producers.

Each adapter reads its source **read-only** and normalizes records into the
shared OKF writer (`engine.knowledge.write_concept`). Adapters never read
`gbrain`; they only write (docs/okf-write-contract.md §6).
"""

from __future__ import annotations

from .caldav import DavIngest, DavIngestStats
from .dav_client import CalEvent, Contact, DavClient
from .exports import ExportsIngest, ExportsIngestStats
from .imap import ImapIngest, ImapIngestStats
from .immich import ImmichIngest, ImmichIngestStats
from .immich_client import ImmichAsset, ImmichClient, ImmichPerson, RestImmichClient
from .jellyfin import (
    JellyfinIngestStats,
    JellyfinItem,
    JellyfinMusicClient,
    JellyfinMusicIngest,
    RestJellyfinMusicClient,
)
from .obsidian import ObsidianIngest, ObsidianIngestStats
from .obsidian_reader import ObsidianReader, VaultNote, VaultObsidianReader
from .paperless import push_companion, push_uploads
from .paperless_client import PaperlessClient, RestPaperlessClient
from .runner import run_ingest


__all__ = [
    "run_ingest",
    "ImmichIngest",
    "ImmichIngestStats",
    "ImmichAsset",
    "ImmichPerson",
    "ImmichClient",
    "RestImmichClient",
    "JellyfinMusicIngest",
    "JellyfinIngestStats",
    "JellyfinItem",
    "JellyfinMusicClient",
    "RestJellyfinMusicClient",
    "ImapIngest",
    "ImapIngestStats",
    "ExportsIngest",
    "ExportsIngestStats",
    "DavIngest",
    "DavIngestStats",
    "CalEvent",
    "Contact",
    "DavClient",
    "ObsidianIngest",
    "ObsidianIngestStats",
    "ObsidianReader",
    "VaultNote",
    "VaultObsidianReader",
    "PaperlessClient",
    "RestPaperlessClient",
    "push_companion",
    "push_uploads",
]
