# ADR 0013 — `/thinking` retires with the per-turn `think` switch

**Status:** Proposed · contingent on ZA-15/E3 landing (see
[`solaris-zielarchitektur.md`](../solaris-zielarchitektur.md), itself status
"Entwurf zur Umsetzung" — draft, not yet implemented) · once in effect,
supersedes the `/thinking` entry in the command list of
[ADR 0009](0009-command-surfaces-control-and-tool.md)

**Not yet in effect.** `/thinking` is still fully wired today
(`solaris-chat/src/solaris_chat/static/index.html`) — this ADR records the
decision for when ZA-15/E3 lands, it does not describe the current system.

## Context

ADR 0009 lists the `/`-Control surface's commands, `/thinking` among them: it lets a
resident show, collapse or hide the model's thinking blocks. It exists because the
engine has a **per-turn `think` switch** — the same switch that backs the two chat
modes, *Zuhause* (fast) and *Solaris Gründlich* (thorough).

The Zielarchitektur removes that switch. **ZA-15** makes the model a property of the
zone rather than a setting, and **ZA-10** turns "fast or thorough?" from a per-turn
choice into a deterministic escalation: the fast loop runs first, and a miss becomes
an asynchronous job. There is no longer a turn that can be thought about harder on
request.

A command whose underlying capability is gone is worse than no command. It either
silently does nothing or has to grow its own fake semantics.

## Decision

**`/thinking` is retired together with the `think` switch.** ADR 0009's command list
loses that one entry; everything else in ADR 0009 — the `/` vs `.` split, the
create-and-find pattern, the menu headings — stands unchanged.

The thinking-block *display* itself is not a per-turn decision and does not need a
command: with `think=false` in zone 1 there are no thinking blocks to show.

## Consequences

- Removing the command is part of backlog item **E3**, not a separate change. It has
  to land with the switch, not before it — a command removed early leaves a mode the
  UI can still reach but nothing implements.
- The two chat modes collapse into one. What replaced *Gründlich* is the job layer
  (column C), so the mode selector's replacement is a visible job, not a toggle.
- If a future need for user-visible reasoning appears, it belongs to zone 2 — Hermes
  has its own model configuration (ZA-15) and its jobs are asynchronous by nature.
  That would be a new ADR, not a revival of this one.
