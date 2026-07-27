"""Upload text extraction.

An uploaded PDF/image lands in `users/<uid>/uploads/` beside a companion `.md`
whose body was, until now, just a title + an Obsidian `![[…]]` embed — no real
text. That companion is what the Obsidian ingest projects into an OKF `note`
concept and what FTS + the nightly Bibliothekar librarian read; with no body,
the upload was invisible to search and never keyworded.

This module extracts the document's text and writes it INTO the companion body
so the very next ingest cycle indexes it and the librarian keywords it:

  - `.pdf`: `pdftotext -layout` first (born-digital PDFs). If that yields too
    little text (a scan / no text layer), rasterize with `pdftoppm` and OCR each
    page with `tesseract`.
  - image: `tesseract` directly.

Every subprocess is bounded (timeout) and wrapped: extraction NEVER raises — on
any failure it logs and returns an empty string, so a bad upload degrades to the
old title-only behaviour rather than crashing the upload handler or the ingest.
"""

from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path

from solaris_chat.logging import log

# Page images fed to the vision extractor: enough pages to cover a typical
# insurance/contract letter, rendered at a resolution the VLM can read table
# cells from without ballooning the turn.
_MAX_VISION_PAGES = 4
# 300 dpi so the vision model can read small table cells (a Bausparkonto
# Kontoauszug's amounts) that 150 dpi left too blurry.
_VISION_DPI = "300"
# Longest-edge pixels for the downscaled page image the paperless push (#931)
# feeds to gemma4:12b: the PoC found the 300-dpi ~5-8 MB pages return an EMPTY
# vision response, while a ~300 KB downscaled image transcribes cleanly. 1500 px
# keeps a text page well under that while staying legible.
_DOWNSCALE_LONG_EDGE = "1500"

# First N pages only — a huge scan must not turn one upload into a minutes-long
# OCR job on the ingest thread.
_MAX_PAGES = 20
# Born-digital heuristic: pdftotext on a scanned PDF returns near-nothing, so a
# short result means "no text layer → fall back to OCR".
_MIN_PDF_TEXT_CHARS = 200
# Cap the companion body so one huge document can't bloat the vault / FTS.
_MAX_TEXT_CHARS = 50_000
_TRUNCATION_MARKER = "\n\n[… gekürzt …]"
_OCR_LANGS = "deu+eng"
_TIMEOUT_S = 120
# --psm 1 = automatic page segmentation WITH orientation detection: real-world
# uploads (scans, letters) are often rotated 90/180°, and without OSD tesseract
# reads a rotated page as mojibake. Needs the `osd` traineddata (in the image).
_OCR_PSM = ("--psm", "1")
# A handful of very common DE/EN words. A born-digital PDF with a broken font or
# rotation passes the length check but is mojibake (pdftotext gives `ap obıa'mmm`
# for `www.ergo.de`); such text contains almost none of these, so its absence is
# the signal to fall back to OCR rather than poison the note with junk.
_COMMON_WORDS = frozenset(
    "der die das und ist ein eine für von mit den dem auf sich nicht auch sie "
    "wir bei zur zum sehr the and for you with this that are was".split()
)

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})

# Marker in a companion body meaning "already extracted" — idempotency guard so a
# re-ingest doesn't re-OCR and re-append.
EXTRACTED_MARKER = "<!-- extracted -->"


def _run(cmd: list[str]) -> str:
    """Run `cmd`, return its stdout, or "" on any failure (never raises)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001 — extraction must never crash the caller.
        log.error("ingest.upload_extract.subprocess_failed", cmd=cmd[0], error=str(e))
        return ""
    return result.stdout or ""


def _cap(text: str) -> str:
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    return text[:_MAX_TEXT_CHARS] + _TRUNCATION_MARKER


def _is_readable(text: str) -> bool:
    """True when `text` looks like real prose rather than mojibake.

    A broken font encoding / rotated text layer yields long garbage that clears
    the length threshold; requiring a few common DE/EN words separates it from a
    correct extraction, so a garbled PDF routes to OCR instead of being kept."""
    words = re.findall(r"[a-zA-ZäöüÄÖÜß]{2,}", text.lower())
    if len(words) < 20:
        return False
    return sum(1 for w in words if w in _COMMON_WORDS) >= 5


def _ocr_pdf(path: Path) -> str:
    """Rasterize the first pages of a scanned PDF and OCR each with tesseract."""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = str(Path(tmp) / "page")
        _run(
            [
                "pdftoppm",
                "-png",
                "-r",
                "200",
                "-f",
                "1",
                "-l",
                str(_MAX_PAGES),
                str(path),
                prefix,
            ]
        )
        pages = sorted(Path(tmp).glob("page*.png"))
        parts = [
            _run(["tesseract", str(img), "stdout", "-l", _OCR_LANGS, *_OCR_PSM])
            for img in pages
        ]
    return "\n".join(p.strip() for p in parts if p.strip())


def extract_text(path: Path) -> str:
    """The upload's extracted text, capped, or "" (never raises).

    `.pdf`: pdftotext first, OCR fallback when the text layer is empty/scanned.
    image: tesseract. Anything else: "".
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        text = _run(
            ["pdftotext", "-layout", "-f", "1", "-l", str(_MAX_PAGES), str(path), "-"]
        )
        # A born-digital text layer is used only when it's both long enough AND
        # readable — a broken-font/rotated PDF clears the length bar with junk, so
        # it (and any scan) routes to OCR.
        if len(text.strip()) >= _MIN_PDF_TEXT_CHARS and _is_readable(text):
            return _cap(text)
        return _cap(_ocr_pdf(path))
    if ext in _IMAGE_EXTS:
        return _cap(
            _run(["tesseract", str(path), "stdout", "-l", _OCR_LANGS, *_OCR_PSM])
        )
    return ""


def _sibling_media(companion_md: Path) -> Path | None:
    """The upload's raw file: the companion's stem with a non-`.md` extension."""
    for sibling in companion_md.parent.glob(f"{companion_md.stem}.*"):
        if sibling.suffix.lower() != ".md":
            return sibling
    return None


def companion_images(notes_dir: str, companion_rel: str) -> list[str]:
    """Base64 PNG page images of a companion's source upload, for the vision
    extractor to read tables/layout directly (Tesseract garbles table columns);
    the OCR text stays as a supplement. Empty on any failure — extraction then
    degrades to OCR-text-only. Never raises."""
    try:
        media = _sibling_media(Path(notes_dir) / companion_rel)
        return _page_images(media) if media is not None else []
    except Exception as e:  # noqa: BLE001 — vision is best-effort.
        log.error("ingest.upload_extract.images_failed", error=str(e))
        return []


def _page_images(path: Path) -> list[str]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        with tempfile.TemporaryDirectory() as tmp:
            prefix = str(Path(tmp) / "pg")
            _run(
                [
                    "pdftoppm",
                    "-png",
                    "-r",
                    _VISION_DPI,
                    "-f",
                    "1",
                    "-l",
                    str(_MAX_VISION_PAGES),
                    str(path),
                    prefix,
                ]
            )
            return [
                base64.b64encode(p.read_bytes()).decode()
                for p in sorted(Path(tmp).glob("pg*.png"))[:_MAX_VISION_PAGES]
            ]
    if ext in _IMAGE_EXTS:
        return [base64.b64encode(path.read_bytes()).decode()]
    return []


def companion_media(notes_dir: str, companion_rel: str) -> tuple[bytes, str] | None:
    """The raw upload file behind a companion as `(bytes, filename)`, or None.

    What the paperless push (#931) POSTs verbatim (paperless stores the original,
    OCR-skipped). Never raises — a missing/unreadable file degrades to no push."""
    try:
        media = _sibling_media(Path(notes_dir) / companion_rel)
        if media is None:
            return None
        return media.read_bytes(), media.name
    except OSError as e:
        log.error("ingest.upload_extract.media_read_failed", error=str(e))
        return None


def downscaled_vision_image(notes_dir: str, companion_rel: str) -> str | None:
    """A single ~300 KB base64 PNG of the companion's first page, for the
    paperless push's vision call. The PoC found the full-res page images return
    an EMPTY gemma response, so this renders a small first page instead. Returns
    None on any failure (the push then skips the vision text). Never raises."""
    try:
        media = _sibling_media(Path(notes_dir) / companion_rel)
        if media is None:
            return None
        ext = media.suffix.lower()
        if ext == ".pdf":
            with tempfile.TemporaryDirectory() as tmp:
                prefix = str(Path(tmp) / "pg")
                _run(
                    [
                        "pdftoppm",
                        "-png",
                        "-scale-to",
                        _DOWNSCALE_LONG_EDGE,
                        "-f",
                        "1",
                        "-l",
                        "1",
                        str(media),
                        prefix,
                    ]
                )
                pages = sorted(Path(tmp).glob("pg*.png"))
                if not pages:
                    return None
                return base64.b64encode(pages[0].read_bytes()).decode()
        if ext in _IMAGE_EXTS:
            return base64.b64encode(media.read_bytes()).decode()
        return None
    except Exception as e:  # noqa: BLE001 — the push must never crash on a bad file.
        log.error("ingest.upload_extract.downscale_failed", error=str(e))
        return None


def extract_into_companion(companion_md: Path) -> bool:
    """Extract the sibling upload's text into `companion_md`. Never raises.

    Returns True when the companion was updated, False when it was already
    extracted (idempotent), the media file is missing, or nothing was extracted.
    """
    try:
        content = companion_md.read_text(encoding="utf-8")
    except OSError as e:
        log.error(
            "ingest.upload_extract.read_failed", path=str(companion_md), error=str(e)
        )
        return False
    if EXTRACTED_MARKER in content:
        return False
    media = _sibling_media(companion_md)
    if media is None:
        return False
    text = extract_text(media)
    if not text.strip():
        return False
    try:
        companion_md.write_text(
            f"{content}\n\n{EXTRACTED_MARKER}\n## Inhalt (extrahiert)\n\n{text}\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.error(
            "ingest.upload_extract.write_failed", path=str(companion_md), error=str(e)
        )
        return False
    return True


def ingest_uploads(notes_dir: str) -> int:
    """Extract text into every upload companion missing it. Never raises.

    Globs the per-resident and shared upload folders, runs
    `extract_into_companion` on each, and returns the count updated this pass.
    """
    root = Path(notes_dir)
    companions = sorted(
        {*root.glob("users/*/uploads/*.md"), *root.glob("uploads/*.md")}
    )
    updated = 0
    for companion in companions:
        try:
            if extract_into_companion(companion):
                updated += 1
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the pass.
            log.error(
                "ingest.upload_extract.companion_failed",
                path=str(companion),
                error=str(e),
            )
    log.info("ingest.upload_extract", companions=len(companions), updated=updated)
    return updated
