"""Skill-run + correction logging. See README.md."""

from .core import (
    NEGATION_PREFIXES,
    append_run,
    detect_correction,
    looks_like_correction,
)

__all__ = [
    "NEGATION_PREFIXES",
    "append_run",
    "detect_correction",
    "looks_like_correction",
]
