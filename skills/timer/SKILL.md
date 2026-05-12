---
name: oscar-timer
description: Use when the user wants to set, list, or cancel a *timer* — a relative-duration reminder ("5 minutes", "an hour"). For absolute-time wake-ups ("tomorrow at 7"), use the `oscar-alarm` skill instead. Calls the shared `oscar_time_jobs` CLI which writes to `time_jobs` in `oscar-brain.postgres` and registers a HERMES cron job for fire-time delivery.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [timer, time, ha-mcp-adjacent, phase-0]
    related_skills: [oscar-alarm, oscar-light]
---

# OSCAR — timer

## Overview

Relative-duration reminders. "5 minutes", "ten minutes", "halbe Stunde", "an hour". For point-in-time wake-ups (clock-based "7am"), the user wants `oscar-alarm` instead.

The skill stores state in the `time_jobs` table in `oscar-brain.postgres` and uses HERMES's built-in `cronjob` tool to schedule the actual fire event. The shared `oscar_time_jobs` CLI is mounted at runtime via the registry volume; invoke it with `python -m oscar_time_jobs ...`.

## When to use

The user says:
- "Set a 5-minute timer."
- "Stell mir einen Pizza-Timer auf 12 Minuten."
- "How long is left on the pasta timer?"
- "Cancel the laundry timer."

Out of scope:
- "Wake me at 7 tomorrow" → `oscar-alarm`
- "Remind me to call mum tonight" → future `oscar-reminder` skill (Phase 1+), not this one. Reminders carry a message; timers carry only a duration + short label.

## Required env

The HERMES container already has these from the `oscar-brain` template:
- `POSTGRES_DSN` — postgres connection string
- Active harness with `uid` (from the gatekeeper or gateway lookup)
- Active `endpoint` from the same source

## Operating sequence

### Setting a timer

1. Parse the utterance into:
   - `duration` (ISO 8601, e.g. `PT5M`, `PT1H30M`). The LLM should infer this from German or English phrasing.
   - `label` (optional, short — "Pizza", "Tea", "Laundry"). Skip when the user didn't say one.
2. The `endpoint` comes from the active conversation context (`voice-pe:<device>` for voice, `signal:<phone>` for chat).
3. Run:
   ```
   python -m oscar_time_jobs add \
     --kind timer \
     --uid <active-uid> \
     --endpoint <active-endpoint> \
     --duration <ISO duration> \
     [--label <short>]
   ```
4. Parse the JSON output:
   ```
   {"ok": true, "job_id": "...", "fires_at": "2026-05-12T10:05:00+00:00", "label": "Pizza", "target_endpoint": "voice-pe:office"}
   ```
5. Schedule the actual fire via HERMES's `cronjob` tool:
   - schedule: one-shot at `fires_at`
   - prompt: `Run python -m oscar_time_jobs fire --job-id <job_id> and deliver the resulting message text.`
   - delivery target: derive from `target_endpoint`:
     - `signal:<phone>` → platform `signal`, recipient `<phone>`
     - `telegram:<chat>` → platform `telegram`, recipient `<chat>`
     - `voice-pe:<device>` → currently no auto-delivery; the fire will return `delivery_pending: voice-pe` and you should leave it for the follow-up gatekeeper-push to handle
6. Confirm short verbally: "OK, 5 Minuten." / "Pizza-Timer läuft."

### Listing timers

```
python -m oscar_time_jobs list --uid <active-uid> --kind timer
```

Returns `{"ok": true, "jobs": [...]}`. Summarise short ("Du hast einen Pizza-Timer in 3 Minuten und einen unbenannten Timer in 12 Minuten."). Don't read the UUIDs aloud.

### Cancelling

```
python -m oscar_time_jobs cancel --uid <active-uid> --label <label>
```

(Or `--job-id <uuid>` if you uniquely identified one from `list`.) Cancellation also removes the corresponding HERMES cron job — use HERMES's `cronjob` tool to delete the matching scheduled job (it's the one whose prompt mentions `--job-id <uuid>`).

## Failure paths

- Postgres unreachable → `python -m oscar_time_jobs` exits non-zero with a one-line stderr message. Tell the user briefly: "Ich kann gerade keinen Timer setzen." Don't expose the DSN.
- User said "5 timer" or some garble that doesn't parse to a duration → ask back: "Wie lange?" — don't guess.
- Label collides with an existing armed timer → the new timer still gets created (multiple-Pizza-timers is a real use case). Confirm with both endings: "Du hast jetzt zwei Pizza-Timer."

## Phase mapping

| Phase | Delivery |
|---|---|
| **0** | Set + list + cancel work. Fire on `voice-pe:` endpoints returned `delivery_pending: voice-pe` — DB updated, no audio. Fire on `signal:` / `telegram:` worked E2E. |
| **1 (now)** | Voice-PE push: the gatekeeper exposes `POST http://oscar-voice:10750/push` accepting `{"endpoint": "voice-pe:<device>", "text": "<message>"}` (bearer-auth via `PUSH_TOKEN`). When fire returns `delivery_pending: voice-pe`, the scheduled cronjob prompt should POST to that endpoint instead of dropping the payload. |
| **1** | Snooze (`timer.snooze`) after a fired timer — "Noch 5 Minuten." Re-arms the job at `now() + duration`. |
| **2** | Strict harness filter (guest vs personal owners). |
| **4** | Multi-room: target_endpoint becomes a list; broadcast cancel. |

## Trace correlation

Every CLI invocation reads `OSCAR_DEBUG_MODE` and logs via the shared `oscar_logging` library. Include the `trace_id` from the conversation context if available (HERMES env or active session). All log events under this skill use the `skill.time_jobs.*` namespace so they correlate with the gatekeeper/connector logs sharing the same `trace_id`.
