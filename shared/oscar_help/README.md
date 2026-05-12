# oscar_help

Read-only introspection over the skill registry. Reads each
`skills/*/SKILL.md` file, parses the YAML frontmatter, and emits a
compact JSON summary that HERMES can quote when the user asks
"what can you do?" / "welche Fähigkeiten hast du?".

Backs the `oscar-help` skill. Same mount as the other shared
libraries (read-only `/opt/oscar/shared` + `/opt/oscar/skills`),
no Postgres dependency, no network — just file IO.

## CLI

```
python -m oscar_help list           # all skills, default phase filter = none
python -m oscar_help list --tag phase-0
python -m oscar_help describe oscar-light
```

Output is JSON on stdout, structured log lines on stderr (the
standard `oscar_logging` setup).

## Why this isn't an LLM-introspection prompt

HERMES *could* just be asked "list your skills". We use a deterministic
walk of the registry instead because:

- skill availability shouldn't depend on the LLM's working memory
- the list stays correct across LLM swaps (cloud-LLM mode, model upgrades)
- the help-skill answer must work when the LLM is offline (status probe
  fallback)

This library is the source of truth — every other consumer (help skill,
ingestion classifier, future skill-curation tooling) reads from here.
