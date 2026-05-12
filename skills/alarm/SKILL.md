---
name: oscar-alarm
description: Use when the user wants to set, list, or cancel an *alarm* — a wake-up or reminder bound to an absolute time ("at 7", "tomorrow morning", "every weekday at 6:30"). For relative-duration reminders ("in 5 minutes"), use `oscar-timer` instead. Shares the `time_jobs` Postgres table and the `oscar_time_jobs` CLI with the timer skill.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [alarm, time, phase-0]
    related_skills: [oscar-timer, oscar-light]
---

# OSCAR — alarm

## Overview

Clock-based wake-ups and reminders. One-shot ("morgen früh um 7", "today 17:30") or recurring ("Werktags 6:30", "every Sunday at 9am").

Same backing store as `oscar-timer` (`time_jobs` in `oscar-brain.postgres`), same CLI tool (`python -m oscar_time_jobs`), different argument shape (`--at` or `--rrule` instead of `--duration`).

## When to use

The user says:
- "Weck mich morgen um sieben."
- "Set an alarm for 6:30 every weekday."
- "Welche Wecker sind gestellt?"
- "Lösch den Werktags-Wecker."

Out of scope:
- "Timer für 5 Minuten" → `oscar-timer`
- "Erinner mich daran, X zu tun" → planned `oscar-reminder` skill (Phase 1+); reminders carry message bodies, alarms only carry a short label

## Operating sequence

### Setting a one-shot alarm

1. Parse the utterance into an absolute datetime in **local time** (the household's `TZ`). Convert to UTC ISO-8601 for the CLI.
2. Run:
   ```
   python -m oscar_time_jobs add \
     --kind alarm \
     --uid <active-uid> \
     --endpoint <active-endpoint> \
     --at <ISO-8601 datetime, UTC> \
     [--label <short>]
   ```
3. Schedule the HERMES cron job pointing at `python -m oscar_time_jobs fire --job-id <job_id>` (same pattern as `oscar-timer`).
4. Confirm: "Wecker für morgen um 7." (read time in local TZ).

### Setting a recurring alarm

Same as one-shot but with `--rrule` instead of `--at`. RFC-5545 RRULE strings:
- "Werktags um 6:30" → `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=6;BYMINUTE=30;BYSECOND=0`
- "Jeden Sonntag 9 Uhr" → `FREQ=WEEKLY;BYDAY=SU;BYHOUR=9;BYMINUTE=0;BYSECOND=0`
- "Jeden Tag 7 Uhr" → `FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0`

The CLI computes the *first* fire time and creates a HERMES cron job for it. After fire, the script re-arms automatically with the next RRULE occurrence.

### Listing and cancelling

```
python -m oscar_time_jobs list --uid <active-uid> --kind alarm
python -m oscar_time_jobs cancel --uid <active-uid> --label <label>   # or --job-id <uuid>
```

When cancelling, also remove the corresponding HERMES cron job via HERMES's `cronjob` tool (match by job-id in the prompt).

## Failure paths

- Ambiguous time ("um sieben") → ask back: "Heute oder morgen Abend?" Don't guess.
- Past timestamp ("Weck mich gestern um 7") → respond "Das ist schon vorbei." Don't insert anything.
- Postgres unreachable → "Ich kann gerade keinen Wecker stellen."

## Phase mapping

| Phase | Delivery |
|---|---|
| **0 (now)** | Set + list + cancel for one-shot and RRULE alarms. Fire on `voice-pe:` returns `delivery_pending: voice-pe`. `signal:` / `telegram:` work end-to-end. |
| **1** | Voice-PE push (same gatekeeper endpoint as `oscar-timer`). |
| **1** | Snooze (`alarm.snooze`) defaults to PT9M. |
| **2** | Strict harness filter. |
| **4** | Multi-room (broadcast wake-up). |

## Sound

TTS-only in Phase 0 — "Guten Morgen, es ist sieben." No Piper-prerendered ringtone (architecture decision, see `docs/timer-and-alarm.md` settled decisions).
