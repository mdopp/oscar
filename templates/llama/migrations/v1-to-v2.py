#!/usr/bin/env python3
"""
Migration: llama v1 → v2.

This is a config/pod-only hop, no on-disk data migration.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("Llama v1 → v2: config/pod-only hop, no on-disk data migration.")
    print("  - llama-server now binds 0.0.0.0 instead of 127.0.0.1 (#1344).")
    print("  - LLAMA_PORT gained blockLanAccess: true, closing the LAN side")
    print("    at the host firewall while sibling pods reach it via")
    print("    host.containers.internal.")
    print("  - The lease broker now passes a request's holder straight")
    print("    through to acquire (#1347).")
    print("  Nothing to move or transform on disk; proceeding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
