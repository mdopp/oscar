"""OSCAR dependency probes — wait-for-ready + doctor."""

from .checks import Check, CheckResult
from .runner import check_all, wait_for_ready

__all__ = ["Check", "CheckResult", "wait_for_ready", "check_all"]
__version__ = "0.1.0"
