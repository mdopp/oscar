---
name: solaris-admin-diagnose
description: The infra investigator — resolves a service name to its container(s) and drills service → container → logs/health via the servicebay_admin MCP without asking for the container name. Read-only.
kind: skill
scope: admin
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — admin diagnose

The operator soul's investigator. When something on the box misbehaves, exhaust
ServiceBay-MCP introspection **before asking the human anything**: the operator
says "Jellyfin", the soul resolves that to the container(s) and reads their logs
itself. Read-scoped `servicebay_admin` tools only — for acting, hand off to
`solaris-admin-act`.

## When to use

- "Schau dir mal Jellyfins Logs an." / "Look at the Jellyfin logfiles."
- "Warum läuft der Media-Stack nicht?" / "Why is the media stack down?"
- "Irgendwas hängt auf der Box — find raus was."
- Any "what's wrong / why is X failing" needing service/container/log/health detail.

Out of scope: acting on the diagnosis (`solaris-admin-act`); household health
summaries (`solaris-status`).

Resolve service names to containers per the operator soul's service↔container
model; never ask for a container name.

## Operating sequence

1. **Locate the service** (`list_services`); if absent, say so and offer the
   closest matches — don't guess wildly.
2. **Resolve to container(s)** (`list_containers`, filter to the service).
3. **Read the symptom:** logs → `get_logs` with `source="container"` (or
   `source="service"` for the whole systemd unit); health → `get_health_checks`
   then `diagnose <check-id>` for a red one; config → `get_service_files`.
4. **Read, don't dump.** Scan for the actual error (trace, non-200, restart loop,
   OOM, missing-env) and summarise in 1–3 sentences naming service, container, and
   the concrete failure.
5. **Decide the next move.** If the fix is an action, name it and hand to
   `solaris-admin-act`; for read-only drilling, continue or defer to
   `solaris-admin-logs` for a focused `since`/grep loop.

## Tool cheat sheet

| Goal | servicebay_admin tool |
|---|---|
| List services + status | `list_services` |
| Map services → containers | `list_containers` |
| One container's logs | `get_logs(source="container", container=<service>-<app>)` |
| A whole service's logs | `get_logs(source="service", name=<service>)` |
| Aggregated health | `get_health_checks` |
| Deep-dive one health check | `diagnose <check-id>` |
| What a service was deployed with | `get_service_files` |

## Failure paths

- `servicebay_admin` unreachable → "Ich erreiche ServiceBay gerade selbst nicht."
- Service named but absent → name it as not-deployed, offer nearest matches.
- Container resolved but logs empty → say it's up but quiet (crash-before-log or
  not-yet-started); check `get_health_checks` next.
