---
name: oscar-identity-link
description: Use when the user (an admin) explicitly asks to link a phone number, chat ID, email, or Discord ID to a known LLDAP uid — e.g. "Verknüpfe die Signal-Nummer +49 151 1234 5678 mit dem User anna" or "Add this Telegram chat id to the household for kind". Writes a row to the `gateway_identities` table in `oscar-brain.postgres`. Admin-only — never invoke from a guest harness, never invoke without an explicit user instruction.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [identity, admin, gateway, phase-1, harness]
    related_skills: []
---

# OSCAR — identity.link

## Overview

Bootstraps the phone-number / chat-id → LLDAP-uid mapping that HERMES checks on every inbound message from Signal, Telegram, email, and Discord. Schema and access semantics are fully specified in [`docs/gateway-identities.md`](../../docs/gateway-identities.md).

Phase 1 has no web UI — this voice/chat-driven skill is how the mapping table gets populated.

## When to use

The admin (someone whose active harness contains `groups: [admins]`) says any of:

- "Verknüpfe Signal-Nummer +49 … mit anna"
- "Link the Telegram chat 123456789 to kind"
- "Add my work Signal +49 … to me as well"
- "Tell OSCAR that bob.smith@example.com is bob"

## Hard guards

- **Admin gate.** Before any DB write: confirm the active harness includes the `admins` group. If not, refuse with "Only an admin can link identities" and log `skill.identity_link.refused_non_admin` (warn) with `trace_id` + active uid.
- **Explicit instruction only.** Don't infer link intent from observation (e.g. seeing an unknown number message in). Only act on an explicit admin request.
- **Validate before writing.**
  - `gateway` must be one of `signal | telegram | email | discord`.
  - For `signal` / `telegram`: `external_id` must match E.164 (`^\+[1-9]\d{6,14}$`) for Signal phone numbers, or be a positive integer chat-id for Telegram. Reject and ask back if not.
  - `uid` should exist in LLDAP. Phase 1: there is no LLDAP MCP tool yet, so trust the admin's spelling but log the uid for after-the-fact review. Phase 2 will add the LLDAP check (architecture doc open point #10).
- **Idempotent insert.** Primary key is `(gateway, external_id)`. If the row already exists with the same uid, confirm verbally without re-writing. If it exists with a different uid, ask the admin to confirm overwrite — don't silently change identity.

## Required tools

- Postgres write access to `gateway_identities`. HERMES's `POSTGRES_DSN` env (set by the `oscar-brain` template) points at the same database. Use HERMES's postgres tool to `INSERT INTO gateway_identities (gateway, external_id, uid, display_name, created_by, verified_at) VALUES (...) ON CONFLICT (gateway, external_id) DO ...`.

## Confirmation pattern

Short verbal confirmation: `"Linked +49 … to anna."` Don't read the number back digit by digit — it's noisy and the admin can check `audit.query`.

Log via `oscar_logging`:

- `skill.identity_link.created` (info) — `trace_id`, `gateway`, `external_id_hash`, `uid`, `created_by`
- `skill.identity_link.exists_same` (info) — same triple already present
- `skill.identity_link.exists_different` (warn) — same (gateway, external_id) but different uid; awaiting overwrite confirmation
- `skill.identity_link.refused_non_admin` (warn) — invoked from a non-admin harness

Never log the raw `external_id` (phone number) at `info` — hash it for privacy. Full `external_id` appears in logs only at `debug` (i.e. `debug_mode=true`).

## Phase mapping

| Phase | What this skill does |
|---|---|
| **1 (now)** | Voice/chat-driven INSERT into `gateway_identities`. Admin permission is the only gate. No LLDAP uid existence check (trust the admin). |
| **2** | Add LLDAP uid existence check via LLDAP-MCP (when that tool lands — architecture doc open point #10). Optional verification-code roundtrip via the target gateway. |
| **2+** | Web UI in the gatekeeper-admin surface takes over routine linking; this skill stays as a quick voice shortcut. |

## Roll-out order (per issue #5)

Michael first. After two weeks of the Signal gateway running without re-pairing or lost messages, link the rest of the family one number at a time. Don't bulk-link.

## Open follow-ups

- LLDAP-uid validation (Phase 2, blocked on an LLDAP MCP tool).
- Cross-channel verification roundtrip ("I just sent you a 6-digit code, say it out loud") — gateway-identities spec lists this as an open question; not in Phase 1.
- Sister-skill `identity.unlink` for removing rows when a user changes numbers. Phase 1 deferred — manual DB edit until needed.
