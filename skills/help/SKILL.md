---
name: oscar-help
description: Use when the user asks "what can you do?", "welche Skills hast du?", "hilf mir, wie sage ich…", or any other "what's possible / what's installed" question. Lists OSCAR's registered skills with one-line descriptions by reading the skill registry directly — deterministic answer that works even when the LLM is unsure. Read-only.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [help, meta, observability, phase-1]
    related_skills: [oscar-status, oscar-audit-query]
---

# OSCAR — help

## Overview

Self-describing capability list. Backed by the shared `oscar_help`
library, which reads each `skills/<name>/SKILL.md` frontmatter from the
registry mount and returns a compact JSON list. Useful as a fallback
when the LLM hasn't loaded the full skill catalog, when a new user
wants to know what they can ask, or when debugging "why isn't OSCAR
doing X?" — if X isn't in the help list, no skill is registered for it.

## When to use

- "Was kannst du eigentlich?" / "What can you do?"
- "Welche Skills sind installiert?"
- "Wie sage ich denn, dass…?" (capability discovery)
- "Gibt es eine Funktion für…?"
- Internal: when a different skill returns "unknown action" and you want to suggest alternatives.

## Operating sequence

### Full list

```
python -m oscar_help list
```

Returns JSON of `{name, description, version, tags, related_skills, path}`
for every skill. Quote the names and the first sentence of each
description verbally — don't read the full descriptions; they're
written for the LLM (skill-routing prompt), not for the user.

### Filtered list

```
python -m oscar_help list --tag phase-0
python -m oscar_help list --tag admin       # admin-only skills
python -m oscar_help list --tag observability
```

Useful when the user asks a specific shape — "Welche Admin-Befehle gibt
es?", "Was kann ich mit der Beobachtung machen?".

### One skill in detail

```
python -m oscar_help describe oscar-light
```

Returns the same record for one skill. Use this when the user asks
"Wie funktioniert X?" — the description is structured for the LLM, so
paraphrase it in user-facing language rather than dumping it raw.

## Failure paths

- `skills/` mount missing or empty → `oscar_help` returns `[]`. Respond
  "Ich habe gerade keine Skills geladen — irgendwas im Deployment ist
  falsch konfiguriert." Don't try to enumerate from memory; that's
  exactly what this skill exists to avoid.
- One skill's frontmatter is malformed → it's silently dropped from the
  list. The CI test `test_real_registry_is_parseable` should catch this
  before deploy; if it slips through, run `python -m oscar_help describe
  <name>` to see which one parses and which doesn't.

## What this does NOT cover

- **Skill availability per harness.** A skill being in the registry
  doesn't mean every harness can call it. Admin-tagged skills only work
  inside the admin harness — that filter lives in the harness composer,
  not here.
- **Whether the skill works *right now*.** For runtime health, use
  `oscar-status`. A registered skill can still fail because Postgres is
  down, the HA token expired, etc.

## Phase mapping

| Phase | Notes |
|---|---|
| **1 (now)** | Lists every committed `skills/*/SKILL.md`. |
| **2** | Filter by `permissions` of the active harness — guests won't see admin skills. |
| **3+** | Include connector-MCP capabilities (so "what cloud LLM is available?" gets a complete answer). |
