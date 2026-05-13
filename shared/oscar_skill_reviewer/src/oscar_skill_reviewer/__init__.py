"""Autonomous correction-driven skill editing — see README.md."""

from .core import (
    K_THRESHOLD,
    REVIEWER_RATE_LIMIT_S,
    CorrectionGroup,
    aggregate_corrections,
    can_apply_now,
    mark_group_edited,
)
from .notify import notify_admin_via_signal

__all__ = [
    "K_THRESHOLD",
    "REVIEWER_RATE_LIMIT_S",
    "CorrectionGroup",
    "aggregate_corrections",
    "can_apply_now",
    "mark_group_edited",
    "notify_admin_via_signal",
]
