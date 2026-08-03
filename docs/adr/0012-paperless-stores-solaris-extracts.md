# ADR 0012 — paperless stores, Solaris extracts: the 12b vision pass stays

**Status:** Accepted · supersedes the *"retire the in-Solaris extractor"* clause of
[ADR 0008](0008-documents-live-in-paperless.md)

## Context

ADR 0008 moved documents to paperless-ngx and, as part of that, decided to **retire
the in-Solaris extractor** — "the 12b cron, `document_extract`,
`DOCUMENT_EXTRACTOR_PROMPT`, `companion_images`/vision, and the upload→OCR path". The
premise was that paperless owns OCR: its Tesseract archive pass plus v3's native
local-Ollama suggestions would produce the text Solaris projects.

The PoC (#929) falsified the premise on real documents. paperless's Tesseract
**regresses rotated German scans to garble** — worse than what the 12b vision pass
already produced. Running both would have meant projecting the worse text.

So the rollout was rescoped (#934, 2026-07-23): paperless's own OCR archive pass is
disabled (`PAPERLESS_OCR_MODE=auto`, `PAPERLESS_ARCHIVE_FILE_GENERATION=never`) and
the `PaperlessIngest` adapter **PATCHes paperless's `document.content`** with clean
text transcribed by the existing `gemma4:12b` vision extractor. A second box finding
(2026-07-24) confirmed paperless's AI classifier reads that same `content` field, so
correspondent/doc-type suggestion works on the pushed text without paperless ever
running OCR itself.

That rescope was never written down as a decision — it lived in an epic comment while
ADR 0008 still said the opposite. This ADR closes that gap.

## Decision

**paperless owns storage, search, classification and the human correction UI.
Solaris owns text extraction.**

- The `gemma4:12b` vision extractor **stays permanently** as the text source for
  documents. It is not a transitional measure.
- paperless's own OCR archive pass stays **disabled**. One text per document, from
  one producer — ADR 0002's no-duplication rule applied to text.
- Everything else in ADR 0008 stands unchanged: paperless is the store, Solaris
  ingests projection-only, correspondents/custom fields drive OKF facts, the
  webhook-push shape, the read-only Dokumente portal.

## Relationship to the Zielarchitektur

[`../solaris-zielarchitektur.md`](../solaris-zielarchitektur.md) decides in **ZA-03**
that `gemma4:e4b` is the only model in the **dialogue** path. This ADR is why that
decision is scoped to *dialogue* rather than to the box: `gemma4:12b` leaves the
dialogue path but stays as a **batch** model. It is loaded on demand, never held
resident, and runs only in crons/ingest — never inside a turn.

## Consequences

- The GPU plan keeps a bursty second consumer. Evicting `e4b` costs a ~6.8 s reload
  on the first turn after a document batch, so the extraction cron belongs in a
  maintenance window, not in the evening.
- `templates/ollama/` must keep pulling a vision-capable 12b tag. Its
  `OLLAMA_DEFAULT_MODEL` moving to `e4b` (backlog E2) must not remove the 12b pull.
- Document text quality is now a Solaris concern, not a vendor concern. If the
  extractor regresses, no fallback OCR catches it — that is the accepted price for
  not projecting garble.
- Should paperless's OCR become good enough on rotated German scans, revisiting this
  is a new ADR, not a silent config change.
