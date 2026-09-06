---
name: solaris-problem-summarizer
description: The weekly troubleshooting-KB scheduler — distils recurring problem→solution sequences into a structured Markdown knowledge base unattended.
kind: scheduler
scope: admin
schedule: 30 4 * * mon
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Problem Summarizer (troubleshooting knowledge base)

**Fires:** `30 4 * * mon` (Mondays 04:30, unattended)

Update the troubleshooting knowledge base. This is the unattended weekly run —
no admin is present, so do **not** ask anyone for input. This skill only
*records* solutions; it never runs a fix, restarts a service, or mutates the
system.

## What to do

1. **Gather troubleshooting signal from what you actually have** — do not
   invent problems:
   - Recent error/warn lines from the stack via the `terminal` tool, e.g.
     `podman logs --since 168h solaris-chat 2>&1 | grep -iE "error|warn|fail"`
     (and the equivalent for `solaris-gatekeeper`, `llama`, …). Look for
     *resolved* sequences.
   - Past admin/diagnostic threads recalled from memory, where a problem was
     reported, investigated and fixed.
2. **Extract problem → indicators → solution triples.** Merge near-duplicates
   into one entry; skip transient noise that self-cleared with no action.
3. **Write** to `/opt/data/notes/knowledge-base/troubleshooting.md` with
   `note_write`. If the file exists (the normal case), read it with `notes_read`
   and **merge** — update an existing problem in place, append genuinely new
   ones, never drop an entry without reason. Write **only** this one file.
4. If nothing new surfaced, leave the file untouched rather than inventing
   content.

## Standard entry template

```markdown
---
type: knowledge-base
tags:
  - solaris/troubleshooting
updated_at: {{timestamp}}
---

# Solaris — Troubleshooting Knowledge Base

## {{problem_title}}
- **Problem**: {{what_failed}}
- **Indicators**: {{diagnostic_signs}}
- **Solution**: {{how_it_was_fixed}}
```

Keep the single top heading and the frontmatter; append each problem as its own
`## ` section. Bump `updated_at` on every write. Keep command/file names verbatim.

## Guards

- **Path sandbox**: only write
  `/opt/data/notes/knowledge-base/troubleshooting.md`.
- **No fabrication**: a quiet week gets no new entries.
- **No acting on problems**: reading logs is fine; acting on them is the admin path.
- **Privacy**: record the technical problem/solution, not who reported it.
