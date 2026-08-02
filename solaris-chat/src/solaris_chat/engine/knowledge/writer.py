"""The OKF write-path core: `write_concept(record) -> concept_id`.

Every ingestion adapter calls this. Given a normalized `ConceptRecord` it runs
the five-step contract (docs/okf-write-contract.md §6) as one unit:

  1. resolve/create the entity (alias dedup, per-resident scope);
  2. write/update the OKF concept `.md` (source of truth);
  3. update the `.db` projection (entities/facts/events/event_entities/concepts);
  4. enqueue the whole-concept embedding (only when content_hash changed);
  5. record `ingest_log` (skip the whole write when content_hash is unchanged).

Adapter-agnostic on purpose: the adapter owns source-shape → `ConceptRecord`;
the writer owns OKF + db + embed + log.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from solaris_chat import notes_index

from . import okf, projection
from .embedding import EmbeddingQueue, NullEmbeddingQueue
from .records import ConceptRecord, WriteResult, fact_triple, is_event_type

# `<...>/okf/events/<year>/<leaf>` → its pre-#830 flat form `<...>/okf/events/<leaf>`.
_SHARDED_EVENT_RE = re.compile(r"((?:.*/)?okf/events)/\d{4}/([^/]+\.md)$")


def _deshard_event_path(rel_path: str) -> str:
    """The flat `okf/events/<leaf>.md` a sharded event path shards from (#830)."""
    m = _SHARDED_EVENT_RE.match(rel_path)
    return f"{m.group(1)}/{m.group(2)}" if m else rel_path


class OkfWriter:
    def __init__(
        self,
        *,
        db_path: str,
        notes_dir: str,
        embedding_queue: EmbeddingQueue | None = None,
    ):
        self._db_path = db_path
        self._notes_root = Path(notes_dir)
        self._embeddings = embedding_queue or NullEmbeddingQueue()

    def write_concept(
        self, record: ConceptRecord, *, ingesting_uid: str = ""
    ) -> WriteResult:
        """Run the full write path for one concept; return its `WriteResult`.

        `ingesting_uid` is the default per-resident scope (§6): the uploading
        resident. An explicit `record.resident` (e.g. an Immich shared-asset
        mapping to `household`) wins over it.
        """
        resident = record.resident or ingesting_uid or "household"
        record.resident = resident

        is_event = is_event_type(record.type)
        ref_kind = "event" if is_event else "entity"

        conn = projection.open_conn(self._db_path)
        try:
            # OKF path first — it's independent of the ref id, so a
            # projection-only event can key its id off it below.
            rel_path = okf.okf_path(record)

            # 1. resolve/create the ref id (entity dedup; events are per-ingest).
            if is_event:
                # A projection-only event (an Immich photo) has no `concepts` row
                # to dedup against, so key its id deterministically off the stable
                # OKF path — the same asset re-ingests to the same event row
                # instead of a fresh random id (which would duplicate the event
                # and churn its content_hash). Full events keep the concepts-path
                # dedup + a random id.
                ref_id = self._existing_event_id(conn, record) or (
                    okf.deterministic_id(rel_path)
                    if record.projection_only
                    else uuid.uuid4().hex
                )
            else:
                # An `identity_key` pins identity to a stable key (a document's
                # source upload) instead of the canonical name, so two uploads
                # that share a title don't collide into one entity (data loss)
                # and re-extracting the same upload updates the same one (#doc).
                #
                # A person is the exception (ADR 0010 §1): the same human must be
                # ONE entity across sources, so a person-typed record runs the
                # name/alias match even when it carries a key, and falls back to
                # the key only for a person that doesn't exist yet. Converging
                # can't conflate scopes: `resolve_entity` matches the same `type`
                # and the same `resident_uid`, so it never pulls in a non-person
                # and never merges a private person into another resident's or
                # into the shared household one.
                ref_id = None
                if record.type == "person" or not record.identity_key:
                    ref_id = projection.resolve_entity(
                        conn,
                        type=record.type,
                        canonical_name=record.title,
                        resident_uid=resident,
                        aliases=record.aliases,
                    )
                if ref_id is None:
                    ref_id = (
                        okf.deterministic_id(record.identity_key)
                        if record.identity_key
                        else uuid.uuid4().hex
                    )

            # OKF file text + its content_hash (the re-ingest skip key).
            text = okf.render(record, entity_id=ref_id)
            new_hash = okf.content_hash(text)

            prior_hash = projection.ingest_log_hash(
                conn, record.source, record.external_id
            )
            if prior_hash == new_hash:
                existing = self._existing_concept(conn, ref_id, ref_kind, rel_path)
                conn.commit()
                return existing

            # Provenance policy (ADR 0002/0005): an externally re-ingestable
            # per-item concept is projection-only — it skips the OKF markdown
            # (step 2), the whole-concept embedding + concepts link row (step 4),
            # and lives purely as its .db projection + facts + the ingest_log
            # idempotency marker. This holds for entities (a Jellyfin song) and
            # events (an Immich photo) alike: step 3 below always projects, so a
            # projection-only photo keeps its events-table row + event_entities —
            # only the per-item markdown/embedding go. Album/artist (and non-photo
            # events like a trip) stay full so their RAG embedding survives.
            projection_only = record.projection_only

            if not projection_only:
                # 2. write/update the OKF concept file (source of truth), then
                # keep the FTS index in step with it (#830) — incremental,
                # content-hash gated inside index_note so an unchanged note is a
                # no-op.
                self._write_okf_file(rel_path, text)
                notes_index.index_note(conn, self._notes_root, rel_path)

            # 3. update the .db projection.
            if is_event:
                self._project_event(conn, record, ref_id, resident)
            else:
                self._project_entity(conn, record, ref_id, resident, new_hash)

            if projection_only:
                concept_id = ""
            else:
                # 4. embedding — only reached because the hash changed.
                embed_id = (
                    projection.concept_embedding_id(
                        conn, self._concept_id(conn, ref_id, ref_kind)
                    )
                    or uuid.uuid4().hex
                )
                embed_text = "\n".join(
                    p for p in (record.title, record.description, record.body) if p
                )
                self._embeddings.enqueue(
                    concept_id=ref_id, embedding_id=embed_id, text=embed_text
                )

                concept_id = projection.upsert_concept(
                    conn,
                    ref_id=ref_id,
                    ref_kind=ref_kind,
                    okf_path=rel_path,
                    content_hash=new_hash,
                    embedding_id=embed_id,
                )

            # 5. ingest_log (idempotency marker).
            projection.record_ingest(
                conn,
                source=record.source,
                external_id=record.external_id,
                content_hash=new_hash,
            )
            conn.commit()
        finally:
            conn.close()

        return WriteResult(
            concept_id=concept_id,
            ref_id=ref_id,
            ref_kind=ref_kind,
            okf_path="" if projection_only else rel_path,
            content_hash=new_hash,
            skipped=False,
            embedded=not projection_only,
        )

    # --- helpers --------------------------------------------------------------

    def _project_entity(self, conn, record, entity_id, resident, content_hash) -> None:
        exists = conn.execute(
            "SELECT id FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        projection.upsert_entity(
            conn,
            entity_id=entity_id,
            is_new=exists is None,
            type=record.type,
            canonical_name=record.title,
            resident_uid=resident,
            source=record.source,
            content_hash=content_hash,
            aliases=record.aliases,
        )
        facts = [(r.rel, r.path, None) for r in record.relationships]
        facts += [fact_triple(f) for f in record.facts]
        projection.replace_facts(
            conn,
            subject_entity_id=entity_id,
            resident_uid=resident,
            source=record.source,
            facts=facts,
        )

    def _project_event(self, conn, record, event_id, resident) -> None:
        exists = conn.execute(
            "SELECT id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        projection.upsert_event(
            conn,
            event_id=event_id,
            is_new=exists is None,
            ts=record.event_ts or record.timestamp or "",
            resident_uid=resident,
            kind=record.event_kind or "event",
            source=record.source,
        )
        members: list[tuple[str, str]] = []
        for r in record.relationships:
            # Prefer the OKF link path (`people/anna`); fall back to a bare name
            # so an adapter can reference an entity either way.
            target = projection.entity_id_for_okf_path(
                conn, r.path
            ) or projection.resolve_entity(
                conn,
                type="person",
                canonical_name=r.path,
                resident_uid=resident,
                aliases=[],
            )
            if target is not None:
                members.append((target, r.rel))
        projection.set_event_entities(conn, event_id=event_id, members=members)

    def _write_okf_file(self, rel_path: str, text: str) -> None:
        # rel_path is built from safe_slug() components, so it cannot escape the
        # notes root; resolve-check anyway as defence in depth.
        path = (self._notes_root / rel_path).resolve()
        if not str(path).startswith(str(self._notes_root.resolve())):
            raise ValueError(f"OKF path escapes the vault: {rel_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _existing_event_id(self, conn, record: ConceptRecord) -> str | None:
        """Events have no canonical-name dedup; the stable key is the OKF path
        (date-prefixed slug), so a re-ingest of the same event reuses its id.

        Match by the deterministic leaf filename, not the literal path, so a note
        migrated from the old flat `okf/events/<slug>.md` to the year-sharded
        `okf/events/<year>/<slug>.md` (#830) is still found — otherwise the
        re-ingest would mint a duplicate at the new path."""
        rel_path = okf.okf_path(record)
        # The pre-migration flat form of the same note: `<...>/okf/events/<leaf>`
        # (the sharded path with its `<year>/` segment removed). Matching both the
        # sharded and the flat path exactly keeps the lookup resident-scoped (the
        # full path carries `users/<resident>/`), so it can't cross-match a
        # different resident's note the way a bare `%/<leaf>` LIKE would.
        candidates = [rel_path]
        flat = _deshard_event_path(rel_path)
        if flat != rel_path:
            candidates.append(flat)
        row = conn.execute(
            "SELECT ref_id FROM concepts "
            "WHERE ref_kind = 'event' AND okf_path IN "
            f"({','.join('?' for _ in candidates)})",
            candidates,
        ).fetchone()
        return row["ref_id"] if row else None

    def _concept_id(self, conn, ref_id: str, ref_kind: str) -> str:
        row = conn.execute(
            "SELECT id FROM concepts WHERE ref_id = ? AND ref_kind = ?",
            (ref_id, ref_kind),
        ).fetchone()
        return row["id"] if row else ""

    def _existing_concept(self, conn, ref_id, ref_kind, rel_path) -> WriteResult:
        row = conn.execute(
            "SELECT id, content_hash FROM concepts WHERE ref_id = ? AND ref_kind = ?",
            (ref_id, ref_kind),
        ).fetchone()
        return WriteResult(
            concept_id=row["id"] if row else "",
            ref_id=ref_id,
            ref_kind=ref_kind,
            okf_path=rel_path,
            content_hash=row["content_hash"] if row else "",
            skipped=True,
            embedded=False,
        )


def write_concept(
    record: ConceptRecord,
    *,
    db_path: str,
    notes_dir: str,
    ingesting_uid: str = "",
    embedding_queue: EmbeddingQueue | None = None,
) -> WriteResult:
    """Module-level convenience over `OkfWriter` for one-shot adapter calls."""
    return OkfWriter(
        db_path=db_path,
        notes_dir=notes_dir,
        embedding_queue=embedding_queue,
    ).write_concept(record, ingesting_uid=ingesting_uid)
