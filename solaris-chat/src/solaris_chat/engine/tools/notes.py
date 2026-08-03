"""Notes-vault tools: search, read, and durable fact capture.

The Obsidian vault (`/opt/data/notes`, Syncthing-synced) is the household
knowledge base. `notes_search` greps it, `notes_read` returns one note,
`fact_store` appends a dated fact file (the dynamic-skills policy: facts,
preferences, household routines — never device state). This is also the
engine's retrieval seam: future Immich/CalDAV/chat retrievers register here
as further tools without touching the loop.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from solaris_chat import notes_index, notes_search
from solaris_chat.engine.fuzzy import fuzzy_score, tokens
from solaris_chat.engine.knowledge import okf, projection
from solaris_chat.engine.ollama import OllamaChat, OllamaError
from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.logging import log
from solaris_chat.notes_search import SHARED_UID, _USER_PATH_RE

_MAX_BYTES = 256 * 1024
_MAX_HITS = 8

# Structured-merge scores (#651): alias-exact and event-range hits are high-
# precision, so they outrank fuzzy vault hits; an #topic anchor boosts a fuzzy
# note. Semantic hits (PR 2) carry their raw cosine as the score.
_ALIAS_SCORE = 0.95
_EVENT_SCORE = 0.9
_ANCHOR_BOOST = 0.3
_ANCHOR_BASE = 0.6

# Semantic branch (#651/#650): only run when fuzzy+alias found fewer than this,
# cap the query embed at this many seconds (voice hot path + model-eviction
# risk), and keep only cosines at/above the floor.
_SEMANTIC_FLOOR_HITS = 3
_EMBED_TIMEOUT_S = 2.0
_SEMANTIC_TOP_K = 5
_SEMANTIC_MIN_COS = 0.35
_EMBED_MODEL = "nomic-embed-text"

_ANCHOR_TOPIC_RE = re.compile(r"#([\w/-]+)")
_ANCHOR_PERSON_RE = re.compile(r"@(\w+)")

# Ranked-search blend (#591): the short title is fuzzily matched (typos clear),
# the body contributes a cheap whole-word coverage of the query terms (NO
# SequenceMatcher over full bodies). `_MIN_SCORE` is tuned so a note carrying all
# query terms as body words always clears it (today's exact-AND matches stay a
# superset at the top); only strong partials additionally leak through.
_TITLE_WEIGHT = 0.6
_BODY_WEIGHT = 0.4
_ALL_TERMS_BONUS = 0.1
_MIN_SCORE = 0.2


def _snippet(text: str, present_terms: list[str]) -> str:
    """A ~240-char excerpt anchored on the first present query term, else start."""
    lower = text.lower()
    idx = -1
    for term in present_terms:
        idx = lower.find(term)
        if idx != -1:
            break
    if idx == -1:
        idx = 0
    return text[max(0, idx - 80) : idx + 160].replace("\n", " ")


def _render_okf_hit(
    root: Path, rel: str, caller_uid: str, score: float, snippet: str | None = None
) -> dict[str, Any] | None:
    """Read an OKF file for `title`/`snippet` (defence-in-depth visibility, #576).

    The SQL scope and the path scope agree by construction, but re-checking the
    file's own owner keeps a `resident:`/path leak from surfacing. Returns the
    compact hit dict, or None when unreadable or not visible to the caller."""
    path = (root / rel).resolve()
    if not str(path).startswith(str(root.resolve())) or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not notes_search.is_visible(notes_search.owner_of(rel, text), caller_uid):
        return None
    return {
        "path": rel,
        "title": notes_search._title(text, path.stem),
        "snippet": snippet if snippet is not None else _snippet(text, []),
        "score": round(score, 4),
    }


def build_notes_tools(
    notes_dir: str, uid_getter, db_path: str = "", ollama: OllamaChat | None = None
) -> list[Tool]:
    root = Path(notes_dir)

    def _candidate_rels(query: str) -> list[str]:
        """Vault-relative candidate paths for `query` (#830).

        The FTS5 index covers 100% of the vault, so a keyword hit anywhere in the
        ~99k files surfaces — the 20k-file walk budget of `iter_vault_md` left
        ~80% invisible. Falls back to the prune-bounded walk only when there's no
        db (tests wiring a vault with no engine DB)."""
        if db_path:
            conn = projection.open_conn(db_path)
            try:
                return notes_index.search(conn, query)
            finally:
                conn.close()
        return [str(p.relative_to(root)) for p in notes_search.iter_vault_md(root)]

    def _fuzzy_hits(
        query: str, terms: list[str], caller_uid: str, boost_paths: set[str]
    ) -> dict[str, tuple[float, dict[str, Any]]]:
        by_path: dict[str, tuple[float, dict[str, Any]]] = {}
        for rel in _candidate_rels(query):
            path = (root / rel).resolve()
            if not str(path).startswith(str(root.resolve())):
                continue
            try:
                if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Scope BEFORE scoring (#576): never score what the caller can't see.
            if not notes_search.is_visible(
                notes_search.owner_of(rel, text), caller_uid
            ):
                continue
            title = notes_search._title(text, path.stem)
            # Body contribution: whole-word coverage of the query terms over a SET
            # of the body's word-tokens — cheap, no SequenceMatcher over the body.
            body_words = set(tokens(text))
            present = [t for t in terms if t in body_words]
            body_coverage = len(present) / len(terms)
            if len(present) == len(terms):
                body_coverage += _ALL_TERMS_BONUS
            score = _TITLE_WEIGHT * fuzzy_score(query, title) + _BODY_WEIGHT * (
                body_coverage
            )
            if rel in boost_paths:
                score += _ANCHOR_BOOST
            if score < _MIN_SCORE:
                continue
            by_path[rel] = (
                score,
                {
                    "path": rel,
                    "title": title,
                    "snippet": _snippet(text, present),
                    "score": round(score, 4),
                },
            )
        return by_path

    def _alias_hits(names: list[str], caller_uid: str) -> dict[str, float]:
        """OKF paths whose entity has an exact (case-insensitive) alias in
        `names`, scoped `resident_uid IN (caller, household)`. Path → score."""
        if not db_path:
            return {}
        wanted = [n for n in dict.fromkeys(n.strip() for n in names) if n]
        if not wanted:
            return {}
        conn = projection.open_conn(db_path)
        try:
            paths: dict[str, float] = {}
            for name in wanted:
                rows = conn.execute(
                    "SELECT c.okf_path FROM entity_aliases a"
                    " JOIN entities e ON e.id = a.entity_id"
                    " JOIN concepts c ON c.ref_kind = 'entity' AND c.ref_id = e.id"
                    " WHERE a.alias = ? COLLATE NOCASE"
                    " AND e.resident_uid IN (?, ?)",
                    (name, caller_uid, SHARED_UID),
                ).fetchall()
                for r in rows:
                    paths[r["okf_path"]] = _ALIAS_SCORE
            return paths
        finally:
            conn.close()

    def _event_hits(
        after: str | None, before: str | None, caller_uid: str
    ) -> list[dict[str, Any]]:
        if not db_path or not (after or before):
            return []
        conn = projection.open_conn(db_path)
        try:
            events = projection.events_between(conn, caller_uid, after, before)
        finally:
            conn.close()
        hits: list[dict[str, Any]] = []
        for ev in events:
            okf_path = ev.get("okf_path")
            if not okf_path:
                continue
            participants = ev.get("participants") or ""
            snippet = f"{ev['ts']} {ev['kind']}"
            if participants:
                snippet += f" — mit {participants}"
            hit = _render_okf_hit(root, okf_path, caller_uid, _EVENT_SCORE, snippet)
            if hit is not None:
                hit["date"] = ev["ts"]
                hits.append(hit)
        return hits

    async def _semantic_hits(query: str, caller_uid: str) -> list[dict[str, Any]]:
        """Cosine top-k over `okf_vectors` — the guarded fallback (#651).

        Runs only when the cheaper sources came up short; the query embed is
        capped by `_EMBED_TIMEOUT_S` because it sits on the voice hot path and a
        cold `nomic-embed-text` can evict a chat model. Timeout / OllamaError /
        missing model → degrade to the structured result (empty here)."""
        if not db_path or ollama is None:
            return []
        conn = projection.open_conn(db_path)
        try:
            if not conn.execute("SELECT EXISTS(SELECT 1 FROM okf_vectors)").fetchone()[
                0
            ]:
                return []
            rows = conn.execute(
                "SELECT v.vector, c.okf_path FROM okf_vectors v"
                " JOIN concepts c ON c.embedding_id = v.embedding_id"
                " LEFT JOIN entities en"
                " ON c.ref_kind = 'entity' AND en.id = c.ref_id"
                " LEFT JOIN events ev ON c.ref_kind = 'event' AND ev.id = c.ref_id"
                " WHERE COALESCE(en.resident_uid, ev.resident_uid) IN (?, ?)",
                (caller_uid, SHARED_UID),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return []
        try:
            embeds = await asyncio.wait_for(
                ollama.embed(_EMBED_MODEL, [query]), _EMBED_TIMEOUT_S
            )
        except (TimeoutError, OllamaError, OSError):
            log.info("engine.notes_search.embed_skipped")
            return []
        if not embeds:
            return []
        q = np.asarray(embeds[0], dtype=np.float32)
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return []
        scored: list[tuple[float, str]] = []
        for row in rows:
            vec = np.frombuffer(row["vector"], dtype=np.float32)
            vn = float(np.linalg.norm(vec))
            if vn == 0.0 or vec.shape != q.shape:
                continue
            cos = float(np.dot(q, vec) / (qn * vn))
            if cos >= _SEMANTIC_MIN_COS:
                scored.append((cos, row["okf_path"]))
        scored.sort(key=lambda s: s[0], reverse=True)
        hits: list[dict[str, Any]] = []
        for cos, okf_path in scored[:_SEMANTIC_TOP_K]:
            hit = _render_okf_hit(root, okf_path, caller_uid, cos)
            if hit is not None:
                hits.append(hit)
        return hits

    async def search(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query or not root.is_dir():
            return "[]"
        after = str(args.get("after") or "").strip() or None
        before = str(args.get("before") or "").strip() or None
        # Default-deny per-owner scope (#576): only the caller's own notes plus
        # the shared household pool — never another resident's private note. An
        # unknown caller resolves to `household`, so it sees the shared pool only.
        caller_uid = uid_getter()
        terms = tokens(query)
        if not terms:
            return "[]"

        # Anchors (#651 §1.3): #topic boosts its tagged notes, @person feeds the
        # alias/mention sources with a specific name.
        topics = _ANCHOR_TOPIC_RE.findall(query)
        persons = _ANCHOR_PERSON_RE.findall(query)
        boost_paths: set[str] = set()
        for slug in topics:
            for n in notes_search.notes_for_topic(root, slug, caller_uid):
                boost_paths.add(n["path"])

        merged: dict[str, tuple[float, dict[str, Any]]] = _fuzzy_hits(
            query, terms, caller_uid, boost_paths
        )
        # A #topic note not caught by the fuzzy blend still surfaces (base score).
        for rel in boost_paths:
            if rel not in merged:
                hit = _render_okf_hit(root, rel, caller_uid, _ANCHOR_BASE)
                if hit is not None:
                    merged[rel] = (_ANCHOR_BASE, hit)

        alias_names = list(persons)
        if len(terms) == 1 and not (topics or persons):
            alias_names.append(query)
        for rel, score in _alias_hits(alias_names, caller_uid).items():
            hit = _render_okf_hit(root, rel, caller_uid, score)
            if hit is not None and (rel not in merged or score > merged[rel][0]):
                merged[rel] = (score, hit)
        for name in persons:
            for n in notes_search.notes_mentioning(root, [name], caller_uid):
                if n["path"] not in merged:
                    hit = _render_okf_hit(root, n["path"], caller_uid, _ANCHOR_BASE)
                    if hit is not None:
                        merged[n["path"]] = (_ANCHOR_BASE, hit)

        for hit in _event_hits(after, before, caller_uid):
            rel = hit["path"]
            score = hit["score"]
            if rel not in merged or score > merged[rel][0]:
                merged[rel] = (score, hit)

        # Semantic fallback (#651/#650): only when the cheap sources came up short.
        if len(merged) < _SEMANTIC_FLOOR_HITS:
            for hit in await _semantic_hits(query, caller_uid):
                rel = hit["path"]
                if rel not in merged:
                    merged[rel] = (hit["score"], hit)

        ordered = sorted(merged.values(), key=lambda s: s[0], reverse=True)
        return json.dumps([h for _, h in ordered[:_MAX_HITS]], ensure_ascii=False)

    async def read(args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "")
        path = (root / rel).resolve()
        if not str(path).startswith(str(root.resolve())) or not path.is_file():
            return '{"error": "not found"}'
        text = path.read_text(encoding="utf-8", errors="replace")
        # Per-owner scope (#576): a path is not a capability — a caller may only
        # read their own or shared notes, never another resident's private note.
        # Ownership is path-based (users/<uid>/) then frontmatter; use the
        # vault-relative resolved path so `../`-style args can't dodge the prefix.
        canon = str(path.relative_to(root.resolve()))
        if not notes_search.is_visible(
            notes_search.owner_of(canon, text), uid_getter()
        ):
            return '{"error": "not found"}'
        return json.dumps({"path": rel, "content": text[:8000]}, ensure_ascii=False)

    async def write(args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "").strip().lstrip("/")
        content = str(args.get("content") or "")
        if not rel.endswith(".md"):
            return '{"error": "path must end in .md"}'
        # Reject empty shells (#<n>): a note that is only frontmatter, headings,
        # and `—` placeholders (an untouched daily-chronicle template, or a
        # title-only agent "Internal log: …") is noise, not knowledge. An append
        # of nothing-new is likewise a no-op error.
        if okf.is_empty_note_shell(content):
            return '{"error": "note has no real content (only frontmatter/headings/placeholders) — nothing written"}'
        # Private-by-default (#576): a real resident's writes ALWAYS land inside
        # their own `users/<owner>/` subtree — a model-supplied `users/<other>/x`
        # or a `../<other>/x` traversal must never reach another resident's space.
        # Strip any leading `users/<anyuid>/`, neutralise `..`, then re-root under
        # the caller; household keeps writing to the shared vault root.
        owner = uid_getter()
        if owner != notes_search.SHARED_UID:
            safe_rel = _USER_PATH_RE.sub("", rel)
            base = (root / "users" / owner).resolve()
            path = (base / safe_rel).resolve()
            if not str(path).startswith(str(base) + "/"):
                return '{"error": "path outside the vault"}'
            rel = str(path.relative_to(root.resolve()))
        else:
            path = (root / rel).resolve()
            if not str(path).startswith(str(root.resolve())):
                return '{"error": "path outside the vault"}'
        # Journal-path canonicalization (#709): the daily-chronicle path is
        # prompt-driven, so the small model writes the same day under varying
        # conventions (`journal/<date>.md`, `journal/journal_<date>.md`,
        # `journal/<YYYY>/<date>.md`). Force every journal write to the one
        # canonical `journal/<YYYY>/<YYYY-MM-DD>.md` (rooted at the same owner
        # base) and overwrite in place, so a nightly re-run is idempotent and can
        # never spawn a new variant regardless of the path string passed.
        canon_rel = notes_search.canonical_journal_path(rel)
        if canon_rel is not None:
            base = (
                root if owner == notes_search.SHARED_UID else (root / "users" / owner)
            )
            path = (base.resolve() / canon_rel).resolve()
            rel = str(path.relative_to(root.resolve()))
            args = {**args, "append": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        if bool(args.get("append")) and path.is_file():
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + content.rstrip("\n") + "\n")
        else:
            # Stamp the caller as owner (#576): a model-written note belongs to
            # the resident it was written for, not the shared pool. Without this
            # the note is untagged and (None = shared) visible to everyone.
            # But content that already carries its own frontmatter (an OKF concept
            # file, or any note authored with `---`) is written verbatim (#657) —
            # prepending a second block would demote the caller's frontmatter to
            # body text, silently dropping its type/id/resident keys. Ownership of
            # such a write is still enforced by the path (re-rooted under
            # users/<owner>/ above) and by its own `resident:`/`added_by:` keys.
            body = content.rstrip("\n") + "\n"
            if content.lstrip().startswith("---"):
                note = body
            else:
                note = f"---\nadded_by: {owner}\n---\n\n{body}"
            path.write_text(note, encoding="utf-8")
        return json.dumps({"written": rel})

    async def fact_store(args: dict[str, Any]) -> str:
        fact = str(args.get("fact") or "").strip()
        if not fact:
            return '{"error": "empty fact"}'
        # Private-by-default (#576): a resident's facts live under their own path;
        # household facts stay in the shared `facts/` dir.
        owner = uid_getter()
        facts_dir = root / "facts"
        if owner != notes_search.SHARED_UID:
            facts_dir = root / "users" / owner / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9äöüß]+", "-", fact.lower())[:48].strip("-")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = facts_dir / f"{day}-{slug or 'fact'}.md"
        path.write_text(
            f"---\nadded_by: {owner}\ndate: {day}\n---\n\n{fact}\n",
            encoding="utf-8",
        )
        return json.dumps({"stored": str(path.relative_to(root))})

    return [
        Tool(
            name="notes_search",
            description=(
                "Durchsucht Notizen und Haushaltswissen"
                " (Stichwort, Namen, Bedeutung)."
                " Für Zeitfragen after/before als ISO-Datum setzen."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "after": {"type": "string", "description": "ISO-Datum, ab"},
                    "before": {"type": "string", "description": "ISO-Datum, bis"},
                },
                "required": ["query"],
            },
            handler=search,
            visibility=Visibility.PERSONAL,
        ),
        Tool(
            name="notes_read",
            description="Liest eine Notiz (Pfad aus notes_search).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read,
            visibility=Visibility.PERSONAL,
        ),
        Tool(
            name="note_write",
            description=(
                "Schreibt eine Notiz in den Haushalts-Vault (Markdown)."
                " append=true hängt an eine bestehende Notiz an."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relativ, endet .md"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            handler=write,
            visibility=Visibility.HOUSEHOLD,
        ),
        Tool(
            name="fact_store",
            description=(
                "Speichert einen dauerhaften Fakt über den Haushalt"
                " (Vorlieben, Routinen, Personen — keine Gerätezustände)."
            ),
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            handler=fact_store,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
