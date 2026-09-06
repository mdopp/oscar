#!/usr/bin/env python3
"""
Migration: solaris v1 → v2.

This is a config/pod-only hop, no on-disk data migration.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("Solaris v1 → v2: config/pod-only hop, no on-disk data migration.")
    print("  - The ollama service is retired (#1332). OLLAMA_URL is gone.")
    print("  - LLAMA_EMBED_URL (default http://127.0.0.1:11436) points at the")
    print("    llama template's second llama-server, which now serves")
    print("    nomic-embed-text on /v1/embeddings.")
    print("  - Photo and document descriptions go through llama-server's")
    print("    multimodal projector instead of Ollama's /api/chat.")
    print("  - The /ollama facade the Home Assistant conversation agent calls")
    print("    is UNCHANGED: it is a wire protocol, not the retired service.")
    print("  Nothing to move or transform on disk; proceeding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
