# oscar_skill_runs

Two-table fundament for self-improving skills:

- **`skill_runs`** — one row per executed skill (utterance, response, outcome).
- **`skill_corrections`** — one row per detected "Nein, ich meinte…" follow-up within 30 s.

Both tables are owned by the oscar-brain Postgres (alembic migration `0002_skill_observability`). This library is the thin write-side and the heuristic that turns a follow-up utterance into a correction row.

## CLI (called from inside SKILL.md operating sequences)

```bash
# Log a run after a skill finishes:
python -m oscar_skill_runs append \
  --trace-id $TRACE_ID --uid michael --endpoint voice-pe:office \
  --skill-name oscar-light --utterance "mach das licht an" \
  --response "office light on" --outcome ok

# Probe the previous run for a correction signal (called on every new utterance):
python -m oscar_skill_runs detect \
  --uid michael --endpoint voice-pe:office --utterance "nein, ich meinte dimmen"
# Exits 0 with a written skill_corrections row if a recent run matched a
# negation prefix; exits 1 if no correction was detected.
```

## Negation heuristic

A correction row is written when:
1. The same `(uid, endpoint)` had a `skill_runs` row in the last 30 s.
2. The new utterance starts (case-insensitive) with one of: `nein`, `no`, `stopp`, `stop`, `doch nicht`, `lass das`, `falsch`, `moment`, `quatsch`, `wait`.

If both conditions hit, the row's `status` is `pending` for the reviewer (#41) to pick up later.
