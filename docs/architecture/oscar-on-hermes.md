# OSCAR on top of Hermes Agent — architecture reset

**Status:** Draft / Proposal — May 2026
**Reason:** After deploying for the first time against a real ServiceBay, it became obvious that the bulk of OSCAR Phase-1 work (skill author, reviewer, revert, signal-gateway, oscar_skill_runs, oscar_help, timer/alarm cron, layered skills) is a re-implementation of features [`nousresearch/hermes-agent`](https://github.com/nousresearch/hermes-agent) ships natively. The architecture doc already names Hermes as the agent core ([oscar-architecture.md §3](../../oscar-architecture.md)); we just didn't act on it. This document corrects course.

## What changes

**OSCAR becomes the *household-specific* layer on top of Hermes.** Hermes owns: agent loop, skill system, messaging gateways (Signal/Telegram/Discord/Slack/WhatsApp/Email), cron scheduler, memory (Honcho), MCP-client integration, self-improvement loop. OSCAR adds: the in-home backend (Postgres + Qdrant for domain memory, alembic migrations), the voice-PE pipeline + gatekeeper, MCP connectors specific to the household (HA-MCP-bridge if needed, weather, cloud-llm), Wyoming protocol handling, and household-domain skills in the `agentskills.io` format.

## Mapping: what stays, repurposed, retired

| Component | Old role | New role |
|---|---|---|
| `templates/oscar-brain/` | full agent + DB + Ollama + signal + sidecars | **Backend pod only**: Postgres + Qdrant + Ollama + db-migrate + pg-backup. No HERMES container. No signal-cli, no signal-gateway. |
| `templates/oscar-voice/` | Wyoming pipeline + gatekeeper | **Unchanged** — voice-PE is OSCAR's own territory; Hermes doesn't do Wyoming. |
| `templates/oscar-connectors/` | Weather + cloud-llm MCP servers | **Unchanged** — MCP servers Hermes connects to. |
| `templates/oscar-ingestion/` | Phase-3a household ingestion | **Unchanged** — household-specific. |
| `shared/oscar_logging` | Structured JSON logging | **Keep** — used by connectors + voice + db-migrate. |
| `shared/oscar_db` | alembic migrations of domain schema | **Keep, simplify**: drops `skill_runs`/`skill_corrections`/`skill_edits`/`skill_drafts` migrations (those duplicated Hermes's own learning loop). Baseline + future household-domain migrations stay. |
| `shared/oscar_health` | dependency probes | **Keep** — used by `oscar-status` skill. |
| `shared/oscar_audit` | structured query over OSCAR audit tables | **Keep** — `oscar-audit-query` skill calls it. |
| `shared/oscar_help` | skill-registry introspection | **Retire** — Hermes has `/skills` natively. |
| `shared/oscar_time_jobs` | timer + alarm backing CLI | **Retire** — Hermes-cron replaces. |
| `shared/oscar_skill_runs` | run + correction logging | **Retire** — Hermes' self-improvement loop replaces. |
| `shared/oscar_skill_author` | user-initiated skill edits | **Retire** — Hermes does this natively. |
| `shared/oscar_skill_reviewer` | autonomous skill edits | **Retire** — Hermes does this natively. |
| `signal_gateway/` | Signal inbound + outbound | **Retire** — Hermes messaging gateway replaces both directions. |
| `gatekeeper/` | Wyoming server + voice-PE push | **Keep** — OSCAR-specific. The push endpoint (`POST /push`) becomes a Hermes-MCP tool (or a webhook Hermes calls) when a Hermes cron job needs to deliver to a Voice-PE device. |
| `skills/light/` | Light control via HA-MCP | **Reformat to agentskills.io** — same logic, new file layout. |
| `skills/status/` | Health check via oscar_health | **Reformat to agentskills.io**. |
| `skills/audit-query/` | Read-only audit query | **Reformat to agentskills.io**. |
| `skills/debug-set/` | Toggle debug_mode | **Reformat to agentskills.io** if still needed, or merge into Hermes config commands. |
| `skills/identity-link/` | LLDAP-uid ↔ phone mapping | **Reformat to agentskills.io** OR drop if Hermes' DM-pairing covers it. |
| `skills/timer/` + `skills/alarm/` | one-shot + rrule schedulers | **Retire** — Hermes cron handles cleanly. |
| `skills/help/` | Self-describing skill list | **Retire** — `/skills`. |
| `skills/skill-author/` + `skills/skill-reviewer/` + `skills/skill-revert/` | Skill management | **Retire** — Hermes natively. |

Roughly: **9 of 11 OSCAR-built skills go away**, plus 5 shared libraries, plus the entire `signal_gateway/`. Two skills (`oscar-light`, `oscar-status`) survive and get repackaged. `oscar-audit-query`, `oscar-debug-set`, `oscar-identity-link` survive *if* still needed after we see what Hermes provides.

## Install topology after the reset

```
                      ┌─────────────────────────────────────────┐
                      │            Hermes Agent (host)          │
                      │  - Installed via `install.sh` script    │
                      │  - Gateway: Signal + Telegram + …       │
                      │  - Skills: ours + Skills Hub + native   │
                      │  - Cron, memory (Honcho), self-improve  │
                      │  - LLM provider: OpenRouter / Nous / …  │
                      └─────────────────────────────────────────┘
                                       │
                                       │ MCP / HTTP
                                       ▼
        ┌────────────────────────────────────────────────────────┐
        │              ServiceBay-managed pods                    │
        │                                                         │
        │  oscar-brain        oscar-voice       oscar-connectors  │
        │  ── postgres        ── faster-whisper ── weather (MCP)  │
        │  ── qdrant          ── piper          ── cloud-llm (MCP)│
        │  ── ollama          ── openwakeword                     │
        │  ── db-migrate      ── gatekeeper                       │
        │  ── pg-backup       (Wyoming + push)                    │
        └────────────────────────────────────────────────────────┘
                                       │
                          ┌────────────┴────────────┐
                          │  HA Voice PE devices    │
                          │  (Wyoming to gatekeeper)│
                          └─────────────────────────┘

Plus: HA-MCP and ServiceBay-MCP as external MCP servers Hermes connects to.
```

## Install path (proposed)

1. ServiceBay full stack as before.
2. `oscar-brain` + `oscar-voice` + `oscar-connectors` deployed as ServiceBay templates (registry-cloned or hand-deployed).
3. Hermes installed on the host (or in a thin wrapper container) via the official one-liner:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
   ```
4. Hermes configured with:
   - LLM provider of choice (Nous Portal / OpenRouter / local Ollama at `http://127.0.0.1:11434`)
   - MCP servers: HA-MCP, ServiceBay-MCP, oscar-connector-weather, oscar-connector-cloud-llm
   - Skills: our `skills/` directory cloned/symlinked into Hermes' skills dir, in `agentskills.io` format
   - Messaging gateway: Signal (one-time QR pair)

A small `oscar/install.sh` wrapper drives steps 2–4 idempotently.

## Migration of issues + branches

Closes as superseded:

- #37 (signal-gateway) — Hermes' built-in
- #38 (layered skills mount) — Hermes' Skills Hub
- #39 (skill_runs/corrections schema) — Hermes' self-improvement
- #40 (oscar-skill-author) — Hermes natively
- #41 (oscar-skill-reviewer) — Hermes natively
- #42 (oscar-skill-revert) — Hermes natively
- #52 (HA-MCP auto-config via Signal dialog) — Hermes likely has its own pairing flow; revisit after install
- #54 (HERMES image replacement) — wrong premise; Hermes is host-installed, not a container

Stays open:

- #34 (Voice-PE push endpoint) — OSCAR-specific
- #43 (Voice-PE → wyoming-satellite verification) — OSCAR-specific (was deferred)
- #50 / PR #51 — already merged, light-skill stays tool-name-agnostic
- #53 (voice → oscar-voice auto-replace) — still useful
- mdopp/servicebay#443 (registry-sync needs git) — independent of OSCAR architecture
- #55 (oscar_db alembic-dir fix) — still useful; the simplified oscar-brain pod still has db-migrate.

## Open questions before we start the destructive cleanup

1. **Where does Hermes Agent run?** Native host install vs. thin container wrapping `install.sh`. Native is simpler but breaks the "ServiceBay manages everything" pattern. Container is more uniform but adds an image to maintain.
2. **Voice-PE push endpoint home.** Stays in `gatekeeper` container, called by Hermes via cron — cleanest. Or moved into Hermes' delivery system if/when Hermes adds a Wyoming target.
3. **Skill format conversion.** Hermes uses `agentskills.io` standard. We have `SKILL.md` with our own frontmatter. Need to either (a) write a one-shot converter, or (b) hand-port the surviving 2-5 skills.
4. **Postgres ownership.** Hermes uses its own SQLite for Honcho + session search. OSCAR's Postgres stays for *domain* memory (books / records / cloud_audit / gateway_identities). They're separate concerns — no merge.
5. **Cleanup tactics.** Delete branches outright (`feat/signal-gateway`, etc.), or `git revert` the merged PRs (preserves history)? The user has said "no production env" through this Sunday — leaning toward straight branch deletion + new PRs that remove the code.

## PR sequence (proposed)

Each as a separate PR for clarity, in order:

1. **PR-reset-1**: this document + tracking issue.
2. **PR-reset-2**: remove `signal_gateway/` + signal-gateway from oscar-brain template + tests.yml + build-images.yml.
3. **PR-reset-3**: remove `shared/oscar_skill_author` + `shared/oscar_skill_reviewer` + `shared/oscar_skill_runs` + their tests + their tests.yml entries + skill-author/reviewer/revert SKILL.md files.
4. **PR-reset-4**: remove `shared/oscar_help` + `shared/oscar_time_jobs` + `skills/help` + `skills/timer` + `skills/alarm`. Drop `0002_skill_observability` + `0003_skill_drafts` alembic migrations (replace with a single squashed baseline if Phase 1 already ran them; otherwise straight delete since we have no prod data).
5. **PR-reset-5**: simplify `oscar-brain` template — remove HERMES container, skills-local-init, signal-cli-daemon, all skills-local volume references.
6. **PR-reset-6**: new `oscar/install.sh` (Hermes install + skill symlink + ServiceBay-template deploy). Updated `stacks/oscar/README.md` with new walkthrough.
7. **PR-reset-7**: convert surviving skills to `agentskills.io` format.

Reviewable in chunks. After PR-reset-5 we can do a clean redeploy and validate the data plane works.

## Notes

- The OSCAR-domain Postgres + Qdrant are still useful and not replaced by Hermes. They're for *household-level* memory (Phase 3+: book collection, record collection, document store, experiences). Hermes' Honcho is for *conversation-level* memory. Different layer.
- The Voice-PE/Wyoming side is genuinely OSCAR-specific. Hermes doesn't do voice PE devices, and we shouldn't expect it to. The push endpoint (#34) is one of the few things we built that doesn't get retired.
- Once the reset lands, the OSCAR repo is dramatically smaller. That's the point — less of our own surface to maintain, more leverage from Hermes' ongoing development.
