# Stack `oscar`

End-to-end install walkthrough for OSCAR on top of a ServiceBay full-stack host plus a host-installed [Hermes Agent](https://github.com/NousResearch/hermes-agent).

OSCAR is the **household layer**: data plane (Postgres + Qdrant + Ollama) in `oscar-brain`, voice pipeline in `oscar-voice`, MCP connectors in `oscar-connectors`. **Hermes is the agent layer**: messaging gateways (Signal/Telegram/etc.), skill management, cron scheduler, self-improvement loop, MCP-client integration.

Architecture rationale: [`../../docs/architecture/oscar-on-hermes.md`](../../docs/architecture/oscar-on-hermes.md).

## Prerequisites

- Fedora CoreOS host with ServiceBay v3.16+ installed and the full-stack deployed.
- HA-MCP enabled in your Home Assistant.
- A ServiceBay-MCP bearer token (Settings → Integrations → MCP → Generate token).
- For **gpu-local**: `nvidia-container-toolkit` + CDI on the host.
- For **cpu-local** / **cloud**: nothing extra.

## One-shot install

```bash
git clone https://github.com/mdopp/oscar.git
cd oscar
export SB_URL=http://<your-host>:5888/mcp
export SB_TOKEN=<your-servicebay-mcp-token>
scripts/install.sh
```

Three steps in order, each idempotent:

1. Install Hermes Agent via Nous Research's installer (skipped if `hermes` is already on `$PATH`).
2. Deploy `oscar-brain` via ServiceBay-MCP (renders the template locally, posts kubeContent + yamlContent — works around mdopp/servicebay#443's registry-sync gap).
3. Symlink `skills/` into `~/.hermes/skills/oscar` so Hermes loads OSCAR's skills.

Override individual steps with `SKIP_HERMES=1` / `SKIP_TEMPLATES=1` / `SKIP_SKILL_LINK=1`.

## Post-install

```bash
hermes setup                        # configure model + messaging gateway
hermes mcp add <ha-mcp-url>         # connect HA-MCP
hermes mcp add <servicebay-mcp>     # connect ServiceBay-MCP
hermes                              # start chatting in the terminal
hermes gateway start                # bring up Signal/Telegram listener
```

Hermes' own setup choices (LLM provider, messaging platforms, etc.) are in the Hermes Agent docs: <https://hermes-agent.nousresearch.com/docs/>.

## Manual install

If `install.sh` doesn't fit your setup:

1. Install Hermes via its official installer.
2. Open ServiceBay UI → Settings → Registries → add `https://github.com/mdopp/oscar.git`. Wait for sync (or `scripts/install.sh` workaround if servicebay#443 still bites).
3. Deploy `oscar-brain`, `oscar-voice`, `oscar-connectors` from the wizard.
4. `ln -s "$(pwd)/skills" ~/.hermes/skills/oscar`.

## Smoke test

```
hermes
> /status
```

Or via Signal once paired:

```
[Signal] You: bist du da?
[OSCAR]      Ja — postgres ok, ollama ok, ha-mcp ok.
```

## Open follow-ups

- mdopp/servicebay#443 — ServiceBay container needs `git` for registry sync.
- #34 — HA Voice PE pairing verification.
- #53 — auto-replace `voice` service with `oscar-voice` on deploy.
- Skills format conversion to `agentskills.io` standard (PR-reset-7).
