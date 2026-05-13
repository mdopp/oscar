# oscar-brain

ServiceBay Pod-YAML template: the OSCAR data plane behind Hermes Agent.

**What's in this pod:** Postgres (household domain tables), Qdrant (semantic index, Phase 3a+), Ollama (local LLM runtime — optional), pg-backup sidecar, oscar-db-migrate sidecar.

**What's *not* here anymore:** Hermes itself (host-installed via Hermes' own installer), messaging gateways (Hermes handles Signal/Telegram/etc. natively), skill management, cron schedulers. See [`../../docs/architecture/oscar-on-hermes.md`](../../docs/architecture/oscar-on-hermes.md) for the reasoning.

## Containers

| Container | Image | Purpose |
|---|---|---|
| `postgres` | `docker.io/postgres:16-alpine` | OSCAR household-domain tables (`system_settings`, `cloud_audit`, Phase-3 book/record/document tables). Not for Hermes conversation history — that's Hermes' own SQLite. |
| `oscar-db-migrate` | `ghcr.io/mdopp/oscar-db-migrate:latest` | Runs `alembic upgrade head` on every pod start, then stays alive. |
| `ollama` *(optional)* | `docker.io/ollama/ollama:latest` | Local LLM runtime — Hermes points its model provider at the pod's published port. Skipped in cloud mode (`OLLAMA_ENABLED=""`). |
| `qdrant` | `docker.io/qdrant/qdrant:latest` | Vector store. Empty in Phase 0/1; populated from Phase 3a (book/record/document semantic search). |
| `pg-backup` | `docker.io/postgres:16-alpine` | Weekly `pg_dump`, 4-week retention. |

## Deployment modes

| Mode | `OLLAMA_ENABLED` | `GPU_PASSTHROUGH` | What you tell Hermes (separately) |
|---|---|---|---|
| **gpu-local** | `yes` | `yes` | Point Hermes at `http://<host>:11434` with a real gemma model |
| **cpu-local** | `yes` | empty | Same, but pick a small model (`gemma3:4b` etc.) |
| **cloud** | empty | (ignored) | Configure Hermes' provider directly (Anthropic / Gemini / OpenRouter / Nous Portal) |

Hermes itself is installed by [`scripts/install.sh` in the hermes-agent repo](https://github.com/NousResearch/hermes-agent/blob/main/scripts/install.sh) — *not* deployed via ServiceBay.

## Storage paths

All under `{{DATA_DIR}}/oscar-brain/`:

| Subdir | Contents |
|---|---|
| `postgres/` | OSCAR domain tables (system_settings, cloud_audit, Phase 3 collections). **Backed up weekly.** |
| `postgres-backups/` | `oscar-YYYYMMDD-HHMMSS.sql.gz`, 4-week retention. |
| `ollama/` | Downloaded model blobs. Large, re-downloadable. |
| `qdrant/` | Vector index. Phase 3a+. |

## Smoke tests after deploy

```bash
# Postgres reachable from the host (so Hermes can be)
psql -h localhost -p 5432 -U oscar -d oscar -c 'select 1'

# Ollama reachable
curl http://localhost:11434/api/tags

# Migration ran
psql -h localhost -p 5432 -U oscar -d oscar -c '\dt'
# → system_settings, cloud_audit, alembic_version
```
