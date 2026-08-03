# solaris-chat — the Solaris Engine

The household assistant's agent core and its chat surface, in one small
offline-capable process. The Hermes gateway era is over: the engine speaks
to Ollama's native `/api/chat` directly, owns its sessions in `solaris.db`,
and replaces what used to be three gateway containers, a config sidecar and
a trace proxy.

## What one process owns

- **Agent loop** — streaming `/api/chat` with hand-written, token-lean tool
  definitions (HA device control, timers, web, notes), bounded passes.
  Model + reasoning are **per turn**: `solaris` (fast, `FAST_MODEL`) for the
  household hot path and the night crons, `THOROUGH_MODEL` for the admin
  persona and the nightly Bibliothekar.
- **HA entity registry injection** — controllable domains (id | name | area,
  no live state) ride the system prompt, so a device command needs no
  list-entities round trip. Stable + sorted → prefix-cache friendly.
- **Sessions** in `solaris.db` (`engine_sessions`/`engine_messages`) with
  per-turn compaction (#210) and ownership as a plain column.
- **Native tracing** — every Ollama call recorded at the call site (light
  ring + detail ring, persisted per turn into `session_traces`); the trace
  panel and waterfall work unchanged, without the `:11436` proxy hop.
- **Scheduler** — timers/alarms/reminders in `engine_timers`; firing rings
  the Voice PE speaker via HA `assist_satellite.announce`.
- **Night crons** — daily-chronicle, problem-summarizer, chat-compactor as
  code-defined jobs on the household profile (durable last-run stamps in
  `engine_cron_runs`; idempotent by construction).
- **Admin persona** — operator soul + skill pack as prompt assembly, with
  the `servicebay_admin` MCP toolbox (official `mcp` SDK; token scopes
  read+lifecycle+mutate, minted by the post-deploy).

## The Ollama facade (`/ollama`)

HA 2026.6's `openai_conversation` has no custom base_url, but its `ollama`
integration takes a free URL + Bearer api_key — so the engine exposes a
minimal Ollama-compatible surface and **is** the Assist conversation agent:

- `GET /ollama/api/tags` — lists the profiles as models (`solaris`, plus
  `solaris-guest`/`solaris-enrollment` when wired).
- `POST /ollama/api/chat` — stateless turn; the caller owns the history.
  NDJSON stream or single JSON (`stream: false`).

The Voice PE speaker path: Speaker → HA pipeline (wake on-device, wyoming
whisper STT) → `conversation.solaris` → this facade → wyoming piper TTS →
Speaker. The voice-gatekeeper speaks the same facade for wyoming-satellite
hardware. Timer rings go engine-scheduler → `assist_satellite.announce`.

## SSO

Behind Authelia forward-auth, NPM sets `Remote-User`; the server folds it
into the resident uid — no second login. `SOLARIS_API_KEY` (the facade bearer)
stays server-side. The pod binds loopback; only NPM/HA/the gatekeeper reach
it over the host loopback.

## Environment (the interesting ones)

| Var | Default | Purpose |
|---|---|---|
| `CHAT_HOST` / `CHAT_PORT` | `127.0.0.1` / `8787` | Loopback bind. |
| `SOLARIS_API_KEY` | — | Bearer for the `/ollama` facade. |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | The LLM backend. |
| `FAST_MODEL` / `THOROUGH_MODEL` | `gemma4:e2b` / `gemma4:12b` | The model map. |
| `HASS_URL` / `HASS_TOKEN` | — | HA tools + registry + announce. |
| `SOUL_PATH` | `/var/lib/solaris/SOUL.md` | Household soul (mtime-cached). |
| `ADMIN_SOUL_PATH` / `ADMIN_SKILLS_DIR` | — | Operator persona prompt. |
| `SB_MCP_URL` / `SB_MCP_TOKEN_PATH` | — | servicebay_admin toolbox. |
| `SOLARIS_DB_PATH` | `/var/lib/solaris/solaris.db` | Sessions, timers, traces. |
| `NOTES_DIR` | `/opt/data/notes` | The notes vault (tools + topics). |

## Run

```
pip install -e .
OLLAMA_URL=http://127.0.0.1:11434 HASS_URL=… HASS_TOKEN=… python -m solaris_chat
```
