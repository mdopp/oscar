# oscar-time-jobs

Shared library powering the `timer` and `alarm` HERMES skills.

Talks to the `time_jobs` table in `oscar-brain.postgres` (schema lives in `templates/oscar-brain/template.yml`'s init SQL; spec in [`docs/timer-and-alarm.md`](../../docs/timer-and-alarm.md)).

## Library surface

```python
from oscar_time_jobs import add, cancel, fire, list_for, NextFire

# Set a 5-minute pizza timer from the office voice-PE.
job_id = await add(
    dsn=..., kind="timer", owner_uid="michael",
    duration="PT5M", label="Pizza",
    target_endpoint="voice-pe:office",
)

# Returns dict {message, target_endpoint, kind, label} ready for delivery.
result = await fire(dsn=..., job_id=job_id)
```

## CLI

For HERMES skills calling out to it:

```bash
python -m oscar_time_jobs add    --kind timer --duration PT5M --label Pizza --endpoint voice-pe:office --uid michael
python -m oscar_time_jobs fire   --job-id <uuid>
python -m oscar_time_jobs list   --owner michael
python -m oscar_time_jobs cancel --label Pizza --owner michael
```

`POSTGRES_DSN` env var supplies the connection string; same DSN as HERMES uses.

## Tests

```bash
cd shared/oscar_time_jobs
pytest
```

Library tests stub the DB via a `FakePool` so no real Postgres is needed. RRULE / next-fire arithmetic gets its own tests via `freezegun`.

## Voice-PE delivery is stubbed (v1)

When a job fires with `target_endpoint=voice-pe:*`, `fire(...)` returns the message but does **not** push it to the speaker — the gatekeeper has no "say this now" endpoint yet (separate follow-up issue). For now:

- `signal:` / `telegram:` endpoints: `fire(...)` returns the text; HERMES's cron auto-delivery handles delivery via `HERMES_CRON_AUTO_DELIVER_PLATFORM`.
- `voice-pe:` endpoints: `fire(...)` returns the text + a `delivery_pending: voice-pe` flag. A future PR adds an HTTP push endpoint to the gatekeeper so the fire script can ring the actual speaker.
