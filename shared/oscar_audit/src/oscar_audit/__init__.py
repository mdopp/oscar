"""Read-side library for OSCAR's domain-audit Postgres tables.

Spec: docs/logging.md sections "Reading interface" and "PII in metadata mode".
"""

from .core import query, supported_streams

__all__ = ["query", "supported_streams"]
__version__ = "0.1.0"
