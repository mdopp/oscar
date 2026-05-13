# oscar-hermes

ServiceBay Pod-YAML template wrapping the **upstream** [Hermes Agent](https://github.com/NousResearch/hermes-agent) container.

Image: `docker.io/nousresearch/hermes-agent:latest` (official, multi-arch, ~1.1 M pulls). We don't fork or rebuild — the only thing this template adds is the ServiceBay deployment shape (Pod-YAML, Mustache variables, hostPath volumes, host-network mode).

## What lives here

| Container | Image | Purpose |
|---|---|---|
| `hermes` | `docker.io/nousresearch/hermes-agent:latest` | Agent loop, messaging gateways (Signal/Telegram/Discord/Slack/WhatsApp/Email), MCP clients, skill registry, cron scheduler, Honcho conversation memory, self-improvement loop. All upstream — we just ship the Pod-YAML. |

`hostNetwork: true` mirrors upstream's `docker-compose.yml` — gateways need outbound + webhook routes to each platform without per-platform port mapping.

## Storage

| Path | Contents |
|---|---|
| `{{DATA_DIR}}/oscar-hermes/data` | Everything Hermes persists — `.env` with API keys, session DB, skills, memories, cron-jobs, messaging-platform session state (Signal-cli auth, Telegram bot token, …). The image is stateless; this dir is the household's Hermes brain. Back up like any other domain volume. |
| `{{OSCAR_REGISTRY_DIR}}/skills` | OSCAR's repo-side `skills/` directory, read-mounted into Hermes' skill loader at `/opt/data/skills/oscar`. Adding a skill = adding a file + `git pull` on the registry checkout. |

## Deploy

`scripts/install.sh` deploys this automatically alongside `oscar-brain`. Manual via the ServiceBay wizard works too.

### First-time setup wizard

After deploy, the `gateway run` command exits immediately because no API keys are configured yet. **Do the setup wizard interactively once:**

```bash
ssh <oscar-host>
podman exec -it oscar-hermes-hermes hermes setup
```

The wizard asks for an LLM provider key (OpenRouter / Anthropic / Google / Nous Portal / local Ollama at `http://localhost:11434` if you have oscar-brain in local-LLM mode) and writes everything into `~/.hermes/.env` *inside the container* (= `{{DATA_DIR}}/oscar-hermes/data/.env` on the host).

Then restart the pod:

```bash
systemctl --user restart oscar-hermes.service
```

The gateway now stays up. Verify:

```bash
curl http://localhost:{{API_SERVER_PORT}}/health
# {"status":"ok"}
```

### Connect MCP servers

```bash
podman exec -it oscar-hermes-hermes hermes mcp add http://localhost:8123/mcp_server/sse --token <ha-mcp-token>
podman exec -it oscar-hermes-hermes hermes mcp add http://localhost:5888/mcp           --token <servicebay-mcp-token>
podman exec -it oscar-hermes-hermes hermes mcp add http://localhost:8801               --token <connectors-bearer>  # weather
podman exec -it oscar-hermes-hermes hermes mcp add http://localhost:8802               --token <connectors-bearer>  # cloud-llm
```

### Pair messaging gateways

```bash
podman exec -it oscar-hermes-hermes hermes gateway setup signal
# follow QR-scan flow
podman exec -it oscar-hermes-hermes hermes gateway setup telegram
# paste bot token
```

Sessions persist under `{{DATA_DIR}}/oscar-hermes/data/`.

## OSCAR skills

OSCAR's `skills/` is mounted read-only at `/opt/data/skills/oscar`. On Hermes startup the skill loader picks up:

- `oscar-light` — HA-MCP lighting control
- `oscar-status` — `oscar_health doctor` wrapper
- `oscar-audit-query` — read-only query over OSCAR domain audit tables
- `oscar-debug-set` — admin: toggle debug_mode

Hermes' own Skills Hub continues to provide cross-platform skills (timers, reminders, memory recall, web search, …). The two coexist; OSCAR-side ones are namespaced under `/oscar-*`.

## Smoke test after install

```bash
podman exec -it oscar-hermes-hermes hermes
# in the TUI:
> /skills
# should list both Hermes' built-in skills + the oscar-* entries
```

```bash
curl http://localhost:{{API_SERVER_PORT}}/health -H "Authorization: Bearer ${API_SERVER_KEY}"
```

## Upgrade

Image tag is `latest` with `AutoUpdate=registry`. Upstream pushes a fresh build whenever they merge to main (multiple times daily; see [Docker Hub](https://hub.docker.com/r/nousresearch/hermes-agent)). Podman' rootless auto-update picks it up; restart the pod to land on the new image.

To pin a specific Hermes commit instead, replace `:latest` with `:sha-<commit-sha>` in the template (sha tags are published per build).

## Logs

```bash
podman logs oscar-hermes-hermes              # gateway + agent
podman logs oscar-hermes-hermes | grep '\[dashboard\]'  # if HERMES_DASHBOARD=yes
```

ServiceBay-MCP: `get_container_logs(id="oscar-hermes-hermes")`.

## Open follow-ups

- **Initial setup automation**: today the user runs `hermes setup` via `podman exec`. Once Hermes supports declarative config (env-var-only setup) we wire that into ServiceBay variables.
- **HA Voice PE delivery**: gatekeeper's `POST /push` is the target Hermes' cron-fire system POSTs into. Wire that up in the skill prose.
- **Skill version-pinning**: currently `:latest`. Decide whether to pin Hermes commit-shas in our release process.
