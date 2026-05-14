# Ingestion pipeline code

Python container code for the pipeline container in the `oscar-ingestion` pod.

Stages: pre-processing → classification (Gemma 4 multimodal via Hermes) → enrichment (opt-in via connector) → confirmation dialog → persistence into the `oscar-brain.postgres` domain collection.

Target phase: 3a, incremental roll-out per material type (books → records → audiobooks → documents → experiences).

Architecture: [`../oscar-architecture.md`](../oscar-architecture.md) → "8. Inbound knowledge pipeline".
