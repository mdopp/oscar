# Skills `timer` & `alarm`

> Status: draft, May 2026. Target phase: Phase 0 (basic functionality), extensions from Phase 1/4. Home: `skills/timer/` and `skills/alarm/` (HERMES skills), shared backing table `time_jobs` in `oscar-brain.postgres`.

Two related but distinct skills sharing infrastructure:

| Use case | Trigger semantics | Example utterance |
|---|---|---|
| **timer** | Relative duration from set-time | "Set a 5-minute timer", "Pizza timer for 12 minutes" |
| **alarm** | Absolute point in time, optionally recurring | "Wake me at 7 tomorrow", "Weekdays at 6:30" |

Two skills, shared backing table + dispatch code. Users never think "timer at 7" or "alarm for 5 minutes" — the split mirrors their mental model.

## Architecture anchoring

- **Persistence:** table `time_jobs` in `oscar-brain.postgres`. Owner uid, label, routing endpoint, kind — everything HERMES's own cron doesn't know.
- **Trigger:** HERMES cron scheduler ([`cron/scheduler.py`](https://github.com/NousResearch/hermes-agent/blob/main/cron/scheduler.py)) — **use the existing mechanism, don't rebuild it**. `timer.set` / `alarm.set` create two things: (a) a row in `time_jobs`, (b) a HERMES cron job via the `cronjob` tool with the prompt "call `timer.fire(<job-id>)` / `alarm.fire(<job-id>)`". Cancel removes both. HERMES's cron-job storage in `~/.hermes/cron/jobs.json` is an implementation detail that doesn't bother us.
- **Cron-context constraint:** HERMES disables the toolsets `cronjob`, `messaging`, `clarify` in cron invocations ([docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/cron-troubleshooting.md)). So `*.fire` must not send a message directly — output goes via HERMES auto-delivery (`HERMES_CRON_AUTO_DELIVER_PLATFORM`) for Signal/Telegram endpoints, and via a direct call to the `oscar-voice` service for `voice-pe:*`.
- **Output:** `target_endpoint` is a **routing key**, not necessarily a Voice PE device — see Routing endpoints below. Multi-room fan-out only in Phase 4 ([oscar-architecture.md](../oscar-architecture.md)).
- **Harness scoping:** every job carries `owner_uid`. List/cancel intents filter on `owner_uid = active_harness_uid`. In guest mode you only see/manipulate `owner_uid='guest'` jobs.

## Intents

| Skill | Intent | Phase | Parameters | Example |
|---|---|---|---|---|
| timer | `timer.set` | 0 | `duration` (ISO 8601 duration), `label?` | "5-minute timer" → `PT5M` |
| timer | `timer.list` | 0 | — | "What timers are running?" |
| timer | `timer.cancel` | 0 | `label?` or `id?` | "Stop the pizza timer" |
| timer | `timer.snooze` | 1 | `duration` | "Five more minutes" (after firing) |
| alarm | `alarm.set` | 0 | `at` (ISO 8601 datetime in local TZ) **or** `rrule` (RFC 5545), `label?` | "Tomorrow at 7" → `at=2026-05-13T07:00:00` |
| alarm | `alarm.list` | 0 | — | "What alarms are set?" |
| alarm | `alarm.cancel` | 0 | `label?` or `id?` | "Delete the weekday alarm" |
| alarm | `alarm.snooze` | 1 | `duration` (default `PT9M`) | "Snooze" (while ringing) |

NLU parsing of time expressions is handled by the LLM (Gemma 4-12B) — no custom grammar layer. The skill receives structured parameters from the model, validates them, and writes them to the table.

## Data model

```sql
CREATE TABLE time_jobs (
  id              UUID PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('timer','alarm')),
  owner_uid       TEXT NOT NULL,               -- LLDAP uid or 'guest'
  label           TEXT,                        -- 'Pizza', 'Weekday alarm', NULL
  fires_at        TIMESTAMPTZ NOT NULL,        -- next firing time (also for RRULE)
  rrule           TEXT,                        -- NULL = one-shot; otherwise RFC 5545
  duration_set    INTERVAL,                    -- original timer duration (snooze anchor)
  target_endpoint TEXT NOT NULL,               -- routing key with prefix (see Routing endpoints)
  hermes_cron_id  TEXT,                        -- reference to HERMES cron job, used for cancel
  state           TEXT NOT NULL CHECK (state IN ('armed','firing','snoozed','done','cancelled')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX time_jobs_fires_idx ON time_jobs (state, fires_at) WHERE state IN ('armed','snoozed');
CREATE INDEX time_jobs_owner_idx ON time_jobs (owner_uid, state);
```

### Routing endpoints

`target_endpoint` is the reply address at fire time — same channel the set command came in on (unless the user explicitly says otherwise).

| Prefix | Example | Set source | Fire handler |
|---|---|---|---|
| `voice-pe:` | `voice-pe:office` | Voice-PE Wyoming session | Gatekeeper → Piper → Wyoming stream back |
| `signal:` | `signal:+4915112345678` | Signal gateway in HERMES | HERMES Signal gateway → text message |
| `telegram:` | `telegram:123456789` | Telegram gateway in HERMES | HERMES Telegram gateway → text message |

The set-command path supplies the value: gatekeeper passes `voice-pe:<device-name>` to HERMES on Voice-PE calls, HERMES gateways set the endpoint themselves on Signal/Telegram calls. The fire dispatcher looks at the prefix and calls the right adapter.

## Lifecycle

```
set        →  state=armed, fires_at set, HERMES cron job created
fire-tick  →  state=firing, dispatch to target_endpoint
              (voice-pe → TTS "Your pizza timer is up"; signal/telegram → text)
ack        →  RRULE? → state=armed, fires_at = next RRULE match
              otherwise → state=done
snooze     →  state=snoozed, fires_at = now() + snooze_duration
cancel     →  state=cancelled, HERMES cron job removed
```

Fire ack is implicit: first voice/button response after firing. Without ack within 60 s → another TTS, max. 3 retries, then `state=done` with an "unanswered" note for later `alarm.list`.

## Conversation-call extension

Every HERMES conversation call carries the routing endpoint — gatekeeper on the voice path, HERMES gateways on the chat path:

```
POST /hermes/converse
{ "text": "…", "uid": "michael", "endpoint": "voice-pe:office", "audio_features": {…} }
```

`endpoint` is a Phase-0 required field once `timer`/`alarm` are deployed. Without `endpoint`, HERMES rejects `timer.set` / `alarm.set` ("Where should I ring?"). From Phase 1 onward the Signal/Telegram paths supply their own endpoint, so the skills work without a Voice PE in the room.

## Phase mapping

| Phase | Delivery |
|---|---|
| **0** | `timer.set/list/cancel`, `alarm.set/list/cancel` with one-shot + RRULE alarms. Endpoint always `voice-pe:*`. Voice ack. |
| **1** | Snooze. `signal:*` / `telegram:*` as routing endpoints once the gateways are live — set/receive timers and alarms on the go. |
| **2** | Harness filter strict (before Phase 2 `owner_uid` is always the single family account). Guest mode with its own jobs. |
| **4** | Multi-room: `target_endpoint` becomes a list; alarms can ring "throughout the house". Stopping from any device stops everywhere (broadcast cancel). |

## Settled decisions

- **Own scheduler — no HA timers.** `timer`/`alarm` do **not** use HA-MCP services (`timer.start`, `input_datetime`). Reason: HA in OSCAR is for home control only; schedulers/timers/alarms live in `oscar-brain`. Avoids duplicated state and keeps multi-room routing (Phase 4) clean.
- **Reminders are a separate skill.** "Remind me to do X" will **not** be built into `timer`/`alarm`. A planned `reminder` skill (Phase 1+) will reuse `time_jobs` as its scheduling backend but hold the reminder text + context in its own table. `time_jobs.label` is a short tag only, not a content field.
- **Alarm sound: TTS only.** No Piper-prerendered ringtone in Phase 0. If needed later, a few WAV assets in `oscar-voice`.
- **Endpoint default = set channel.** A timer/alarm set via Signal fires back as a Signal message — not in the house. Set via Voice PE → audio on the same device. Cross-channel routing only on explicit request ("alarm in the living room at 7"). Avoids the UX surprise ("why is my living room ringing, I was in a café").
