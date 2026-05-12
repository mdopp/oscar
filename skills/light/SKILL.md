---
name: oscar-light
description: Use when the user asks to turn lights on or off, dim them, or set a light scene in a specific room or area of the house. Calls HA-MCP `HassTurnOn` / `HassTurnOff` / `HassSetBrightness` against the matching area or entity. First HERMES skill in OSCAR Phase 0 — the E2E proof that voice → gatekeeper → HERMES → HA-MCP → device works.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [home, light, ha-mcp, phase-0]
    related_skills: []
---

# OSCAR — Light control

## Overview

Direct lighting control via the Home Assistant MCP server. This skill exists primarily to verify the Phase-0 end-to-end pipeline (voice in → HA action out). Once the routine flow works for lights, it becomes the template for heating, music, and any other HA-MCP-mediated control.

## When to use

The user wants to:
- turn one or more lights on or off
- dim or brighten a light or a room
- set a quick scene ("warm white", "evening", "100 %", "halb")

Out of scope (different skills):
- timer / alarm setting → `timer` / `alarm` skills
- music playback → music skill (Phase 0)
- HA automation editing → use ServiceBay-MCP for config writes, not this skill

## Required tools

- `HA-MCP` — the bearer-authenticated MCP client wired in by the `oscar-brain` template via env vars `HA_MCP_URL` and `HA_MCP_TOKEN`.
  - `HassTurnOn` — accepts `area` or `entity_id`, optional `brightness_pct` / `color_name` / `kelvin`
  - `HassTurnOff` — `area` or `entity_id`
  - `HassSetPosition` — for blinds/lamps that take a positional value
  - `HassLightSet` — direct light-domain set (less commonly needed when area suffices)

## Operating principles

1. **Prefer `area` over `entity_id`.** Users say "the living room", not `light.living_room_ceiling_left`. Pass the area name verbatim to HA-MCP — HA resolves it.
2. **Trust the LLM to parse, the tool to validate.** Don't pattern-match in this document. The LLM should structure `(area, action, brightness?)` from the utterance and call the right HA-MCP tool. If HA rejects (unknown area, no light entities), respond with the HA error verbatim — short.
3. **Resolve "the light" without a room only via the active endpoint.** If the routing endpoint is `voice-pe:office`, "turn the light on" defaults to area `office`. If the endpoint is `signal:...` or `telegram:...` (mobile chat), no room context — ask back: "Which room?"
4. **Confirm by action, not by speech.** Keep verbal confirmation to a few words ("ok", "office lights on"). The user hears the lights click anyway.
5. **Brightness is a percentage 1-100** in the HA-MCP API. Map "dim", "halb", "low" to ~30; "bright", "full", "voll" to 100; "off" to `HassTurnOff` rather than 0.

## Failure paths

- HA unreachable / HA-MCP returns 5xx → respond "Home Assistant doesn't answer right now"; log via `oscar_logging` as `skill.light.ha_unreachable` (warn).
- Area not found in HA → respond "I don't see a {area} in Home Assistant"; log as `skill.light.area_unknown` (info).
- Bearer rejected (401) → respond "I can't reach Home Assistant"; log as `skill.light.auth_failed` (error). Likely the `HA_MCP_TOKEN` in `oscar-brain` is stale.

In every failure case the user gets a short verbal reason — no troubleshooting prose. Details belong in the structured logs.

## Pre-deployment checks

1. In Home Assistant, areas are named in plain language matching how the family speaks: `Office`, `Living Room`/`Wohnzimmer`, `Kitchen`/`Küche`, `Bedroom`/`Schlafzimmer`. Avoid technical names (`Floor 1 East`).
2. Each area has at least one entity in the `light` domain (not `switch`) — HA-MCP routes only entities marked as lights through the light action tools.
3. The HA-MCP integration is enabled in HA and a long-lived access token is in `oscar-brain` as `HA_MCP_TOKEN`.

## Smoke tests (E2E for issue #3)

These verify the full Phase-0 pipeline, not just this skill:

```
"Hey Jarvis, turn the office light on"      → office light(s) on
"Turn the office light off"                  → office light(s) off
"Dim the kitchen light to thirty percent"    → kitchen at 30 %
"Turn the light on" (spoken in the office)   → office light on (endpoint-derived area)
"Turn the light on"  (sent via Signal)       → "Which room?"
```

In each case `get_container_logs(id="oscar-brain-hermes")` should show a single `trace_id` that ties together `gatekeeper.transcript`, the HA-MCP call, and `gatekeeper.response`. `cloud_audit` must be empty — local Gemma should handle this without escalation.

## Phase 4 forward

Multi-room voice routing changes the "no-room-said → use endpoint room" rule: if the user has visibly walked into another room (presence detection), the endpoint might lag the user's location. Phase 4 will introduce a presence-aware default. Until then, endpoint-room is the right default.
