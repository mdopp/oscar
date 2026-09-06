"""YouTube-Music history → `wishlist` album facts (#868, P3, ADR 0002/0003/0004).

The standalone `solaris-import-google` tool turned a Takeout `watch-history.json`
into a music SHOPPING-LIST NOTE (markdown, delivered over Syncthing). In-engine
the substrate is different: a played album a resident does not yet own is a
QUERYABLE signal on the **album entity**, not a materialized list. So this kind
runs the vendored ``music_shopping.analyze_iter`` (play aggregation, ytmusicapi
album resolution, greedy set-cover, fuzzy ownership — the "prefer unresolved over
wrong" guard is kept) under the durable job runner, and for each resolved album
writes a source-tagged ``wishlist`` fact (source=import) + a ``play_count`` fact
onto the album entity (resolved/created by P1a's ``Artist – Album`` canonical_name
+ ``{artist_slug}-{album_slug}`` slug, so it dedups with Jellyfin's/notes'/chat's
album node — one entity, many sources).

The acquire list is NOT written here: it is the existing ``music_query
op="wishlist"`` (#879), which reads these ``wishlist`` facts and suppresses an
album the resident already owns physically or has digitally. No per-song markdown,
no wishlist note.

Idempotent + per-resident (job owner): a re-run replaces the source=import facts
on the same album node — no duplicate album entities.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solaris_chat.engine.knowledge import ConceptRecord, safe_slug
from solaris_chat.engine.knowledge.writer import OkfWriter

from .. import ImportPlan
from ..music_shopping import analyze_iter
from ..paths import ImporterPaths

# Source tag for an import-derived album fact (ADR 0003 — one entity, many
# sources: it coexists with Jellyfin's `by`/`on_album` and a note's
# `owned_physical`). The wishlist query (#879) reads the `wishlist` predicate.
_SOURCE = "import"


def _write_album_facts(
    writer: OkfWriter, owner_uid: str, artist: str, album: str, plays: int
) -> bool:
    """Resolve/create the album entity by P1a's ``Artist – Album`` canonical_name
    + ``{artist_slug}-{album_slug}`` slug and write source=import ``wishlist`` +
    ``play_count`` facts. Returns True when written (not an unchanged re-run)."""
    try:
        artist_slug = safe_slug(artist)
        album_slug = safe_slug(album)
    except ValueError:
        return False
    rec = ConceptRecord(
        type="album",
        title=f"{artist} – {album}",
        slug=f"{artist_slug}-{album_slug}",
        source=_SOURCE,
        external_id=f"import:{owner_uid}:{artist_slug}-{album_slug}",
        resident=owner_uid,
        facts=[("wishlist", ""), ("play_count", str(plays))],
        # Import owns no markdown for the album — the entity + facts are the
        # signal; a Jellyfin/note ingest keeps the RAG-worthy album markdown.
        projection_only=True,
    )
    return not writer.write_concept(rec, ingesting_uid=owner_uid).skipped


def run_music_import(
    history_bytes: bytes,
    paths: ImporterPaths,
    *,
    owner_uid: str,
    db_path: str,
    notes_dir: str,
    model: str = "",
    is_canceled=None,
    **opts: Any,
):
    """Generator: run ``analyze_iter`` then write ``wishlist``/``play_count``
    album facts for each resolved album the owner played. Yields the analysis
    progress events, then a final event carrying the write summary as ``result``.

    Classification is mechanical (``catalog.classify`` seed list) — no LLM in the
    hot path. ``model`` is accepted (job-payload contract) but the music analysis
    does not use it."""
    result: dict[str, Any] = {}
    for ev in analyze_iter(history_bytes, paths, is_canceled=is_canceled, **opts):
        if "result" in ev:
            result = ev["result"]
        else:
            yield ev
    if is_canceled is not None and is_canceled():
        return

    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir)
    written = 0
    for g in result.get("groups", []):
        if not g.get("resolved") or g.get("category") != "Musik":
            continue
        if _write_album_facts(
            writer, owner_uid, g["artist"], g["album"], int(g.get("plays", 0))
        ):
            written += 1
    yield {
        "stage": "done",
        "message": f"{written} Alben zur Wunschliste",
        "pct": 100,
        "result": {
            "type": "music",
            "albums_written": written,
            "resolved_tracks": result.get("resolved_tracks", 0),
            "missing_songs": result.get("missing_songs", 0),
        },
    }


class MusicImporter:
    """The ``music`` (ytmusic) importer kind (#868). ``run`` drives the album-fact
    write path; ``detect``/``plan`` are the archive-manifest surface the shared
    ``Importer`` protocol requires (a Takeout ``watch-history.json`` claim)."""

    kind = "music"

    # Takeout localises the watch-history filename (watch-history.json EN,
    # Wiedergabeverlauf.json DE, …). The .zip upload path finds it by CONTENT
    # (orchestrator._find_watch_history); this manifest-only protocol method has
    # no content, so it matches the known localized basenames.
    _HISTORY_BASENAMES = ("watch-history.json", "wiedergabeverlauf.json")

    def detect(self, manifest: Any) -> list[dict[str, Any]]:
        names = manifest if isinstance(manifest, list) else []
        if any(
            str(n).replace("\\", "/").rsplit("/", 1)[-1].lower()
            in self._HISTORY_BASENAMES
            for n in names
        ):
            return [{"kind": self.kind, "datatype": "ytmusic"}]
        return []

    def plan(self, archive: Any, selections: Any) -> ImportPlan:
        return ImportPlan(kind=self.kind)

    def run(self, plan: ImportPlan, progress: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("music import runs via the durable job runner")


def _music_runner_factory(payload: dict[str, Any]):
    """Build the durable-job factory for the ``music`` kind from its payload.

    Payload carries ``history`` (the watch-history.json bytes as a UTF-8 or
    latin-1-safe string), the owner + on-disk paths, the LLM endpoint, and the
    bounded ``analyze_iter`` options (min_plays/months/resolve/cap)."""
    history_bytes = payload["history"].encode("utf-8")
    paths = ImporterPaths(
        radicale_data=Path(payload.get("radicale_data", "")),
        music_dir=Path(payload["music_dir"]),
        data_dir=Path(payload["data_dir"]),
    )
    opts = {
        k: payload[k] for k in ("min_plays", "months", "resolve", "cap") if k in payload
    }

    def factory(is_canceled):
        return run_music_import(
            history_bytes,
            paths,
            owner_uid=payload["owner_uid"],
            db_path=payload["db_path"],
            notes_dir=payload["notes_dir"],
            model=payload.get("model", ""),
            is_canceled=is_canceled,
            **opts,
        )

    return factory
