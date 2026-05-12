# oscar-brain

ServiceBay Pod-YAML-Template: HERMES (GPU-fähig) + Ollama (Gemma 4-12B Q4 + Gemma 4-1B Router) + Qdrant + Postgres.

Postgres hostet alle OSCAR-Domain-Tabellen: `zeit_jobs`, `gateway_identities`, `cloud_audit`, perspektivisch `tuersteher_voice_embeddings`, `ingestion_classifications`. Nightly `pg_dump`-CronJob als zusätzlicher Container, eigener Volume-Mount für Dumps (4 Wochen Retention).

Phase 1: zusätzlicher `signal-cli-daemon`-Sidecar für das HERMES-Signal-Gateway.

Architektur: [`oscar-architecture.md`](../../oscar-architecture.md). Logging-Konvention: [`docs/logging.md`](../../docs/logging.md).
