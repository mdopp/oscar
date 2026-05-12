# oscar-audit

Read-side companion to OSCAR's domain-audit Postgres tables. Backs the `audit-query` HERMES skill.

## Streams supported

| Stream | Table | Filters |
|---|---|---|
| `cloud_audit` | `cloud_audit` | `since`, `until`, `uid`, `vendor`, `trace_id`, `min_cost_micro_usd` |
| `gateway_identities` | `gateway_identities` | `since`, `gateway`, `uid` |
| `time_jobs` | `time_jobs` | `since`, `uid`, `kind`, `state` |

Future streams (`gatekeeper_decisions` from Phase 2, `ingestion_classifications` from Phase 3a) plug into the same dispatch table.

## CLI

```bash
python -m oscar_audit query --stream cloud_audit --since 1h --uid michael --limit 20
python -m oscar_audit query --stream cloud_audit --trace-id 11111111-1111-1111-1111-111111111111
python -m oscar_audit query --stream time_jobs --state armed --kind alarm
```

`--since` accepts ISO 8601 (`2026-05-12T08:00:00`) or relative (`1h`, `24h`, `7d`, `today`, `yesterday`).

`POSTGRES_DSN` env var supplies the connection.

## Library

```python
from oscar_audit import query

rows = await query(
    pool,
    stream="cloud_audit",
    since=datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc),
    uid="michael",
    limit=20,
)
```

## Tests

```bash
cd shared/oscar_audit
pytest
```

Uses a FakePool same pattern as `oscar_time_jobs`.

## Privacy

`cloud_audit.prompt_fulltext` and `response_fulltext` are returned only when `OSCAR_DEBUG_MODE=true`. Metadata-only otherwise. The CLI flags this in its output so callers know whether they're seeing redacted data.
