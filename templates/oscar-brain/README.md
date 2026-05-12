# oscar-brain

ServiceBay Pod-YAML template: HERMES (GPU-capable) + Ollama (Gemma 4-12B Q4 + Gemma 4-1B router) + Qdrant + Postgres.

Postgres hosts all OSCAR domain tables: `time_jobs`, `gateway_identities`, `cloud_audit`, eventually `gatekeeper_voice_embeddings`, `ingestion_classifications`. A nightly `pg_dump` CronJob runs as an additional container with its own volume mount for dumps (4 weeks retention).

Phase 1: adds a `signal-cli-daemon` sidecar for the HERMES Signal gateway.

Architecture: [`oscar-architecture.md`](../../oscar-architecture.md). Logging convention: [`docs/logging.md`](../../docs/logging.md).
