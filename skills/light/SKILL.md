---
name: oscar-light
description: Use when the user asks to turn lights on or off, dim them, or set brightness in a specific room or area of the house. Routes the request through the Home Assistant MCP server — Hermes picks whichever HA-MCP tool matches the (area, action, brightness) intent at runtime from the discovered tool catalog.
version: 0.3.0
author: OSCAR
license: MIT
---

# OSCAR — Light control

## Overview

Direct lighting control via the Home Assistant MCP server. This skill exists primarily to verify the Phase-0 end-to-end pipeline (voice in → HA action out). Once the routine flow works for lights, it becomes the template for heating, music, and any other HA-MCP-mediated control.

**Resolution architecture (important to understand before changing this prose):**

- Hermes connects to HA-MCP on boot and gets the **full tool catalog** via `tools/list`. The tools' canonical names (`HassTurnOn` etc. in current HA versions) live in that catalog, *not* in this file.
- This skill describes the *intent* — what we want to happen — and lets Hermes match it to whatever tool HA-MCP currently exposes for "turn lights on/off in an area".
- Entity-level resolution ("Wohnzimmer" → `light.wohnzimmer_decke + light.wohnzimmer_steh`) happens **inside HA**, via HA's Assist intent system. OSCAR passes the area name verbatim; HA finds the entities.

This is why the skill stays correct across HA upgrades: tool names and entity catalogues are HA's problem, not ours.

## When to use

The user wants to:
- turn one or more lights on or off
- dim or brighten a light or a room
- set a brightness level ("100 %", "halb", "thirty percent")

Out of scope (different skills):
- timer / alarm setting → `oscar-timer` / `oscar-alarm`
- music playback → music skill (Phase 0+)
- HA automation editing → use ServiceBay-MCP for config writes, not this skill

## Capability the skill needs from HA-MCP

You don't reference tool names directly — Hermes has them from the live tool catalog. You need *one* HA-MCP tool that can:

- Accept an **`area`** (string, e.g. `"Wohnzimmer"`) or **entity name** parameter.
- Apply an **action** in `{on, off}` plus an optional **brightness percentage** (1–100).
- Return success or a structured error.

In current HA versions that's the `HassTurnOn` / `HassTurnOff` family of Assist intents. If those names change in a future HA release, the tool catalog updates, and this skill keeps working as long as the *capability* is exposed.

## Operating principles

1. **Pass area names verbatim.** Users say "Wohnzimmer", not `light.wohnzimmer_decke_links`. HA's Assist intent system resolves the area; OSCAR doesn't try to enumerate entities.
2. **Trust the LLM to parse, the tool catalog to validate.** Don't pattern-match in this document. The LLM should structure `(area, action, brightness?)` from the utterance and call the matching tool. If HA rejects (unknown area, no light entities), respond with the HA error verbatim — short.
3. **Resolve "the light" without a room only via the active endpoint.** If the routing endpoint is `voice-pe:wohnzimmer`, "mach das Licht an" defaults to area `Wohnzimmer`. If the endpoint is `signal:...` or `telegram:...` (mobile chat), no room context — ask back: "Welcher Raum?"
4. **Confirm by action, not by speech.** Keep verbal confirmation to a few words ("ok", "Wohnzimmer ist an"). The user hears the lights click anyway.
5. **Brightness is a percentage 1-100.** Map "dim", "halb", "low" to ~30; "bright", "full", "voll" to 100; "off" must use the turn-off tool, not brightness=0.

## Failure paths

- HA unreachable / HA-MCP returns 5xx → respond "Home Assistant antwortet gerade nicht."; log via `oscar_logging` as `skill.light.ha_unreachable` (warn).
- Area not found in HA → respond "Das {area} kenne ich in Home Assistant nicht. Lege es dort an oder benenne den Raum um."; log as `skill.light.area_unknown` (info).
- Bearer rejected (401) → respond "Ich erreiche Home Assistant nicht — Token vermutlich abgelaufen."; log as `skill.light.auth_failed` (error). Likely the `HA_MCP_TOKEN` in `oscar-brain` is stale.
- HA-MCP returned the tool catalog at boot but doesn't include a turn-on/off tool (e.g. HA Assist disabled) → respond "Home Assistant hat mir keine Licht-Werkzeuge gegeben."; log as `skill.light.no_capability` (error). The fix is HA-side (enable Assist + expose entities).

In every failure case the user gets a short verbal reason — no troubleshooting prose. Details belong in the structured logs.

## HA-side prerequisites

These live in **Home Assistant**, not OSCAR. Without them the skill can't work even if everything else is wired correctly:

1. **Areas named in household language.** `Wohnzimmer`, `Küche`, `Bad`, `Schlafzimmer`, `Büro` — not `Floor 1 East` or `zone_3`. HA's Assist intent system matches user utterances against these names.
2. **Light entities assigned to areas.** Every light bulb / lamp / strip lives in exactly one HA area. Mixed areas (one entity in two areas) will route ambiguously.
3. **Entities exposed to Voice Assistants.** HA → Settings → Voice Assistants → "Expose" tab. Toggle every light you want voice-controlled. Unexposed entities are invisible to MCP — the same as not having them at all.
4. **`mcp_server` integration enabled.** HA → Settings → Devices & Services → "Add Integration" → MCP Server. The URL goes into `oscar-brain`'s `HA_MCP_URL` variable.
5. **Long-lived access token minted.** HA → user profile → Security → Long-lived access tokens. Paste into `HA_MCP_TOKEN`. Treat it like any other secret — losing it means OSCAR locks out of HA.

If any of these is missing, `oscar-light` will fail at runtime — usually with `skill.light.area_unknown` or `skill.light.no_capability`. The fix is always in HA, not in this skill's prose.

## Inspecting the live tool catalog (debugging)

When something feels off ("OSCAR sagt das Tool kennt es nicht"), inspect what Hermes actually saw from HA-MCP:

```bash
hermes logs mcp | grep -i "ha-mcp\|tools/list"
```

Hermes logs the tool catalog at MCP-handshake time. If the expected tool isn't there, the problem is HA-side (Assist not enabled, no exposed entities, integration broken). If it *is* there but OSCAR still can't use it, the problem is in this skill prose or in Hermes's routing.

## Smoke tests

These verify the full Phase-0 pipeline, not just this skill:

```
"Hey Jarvis, schalte das Wohnzimmerlicht an"  → Wohnzimmer-Licht(er) an
"Mach das Licht im Wohnzimmer aus"            → Wohnzimmer-Licht(er) aus
"Dimm das Küchenlicht auf dreißig Prozent"    → Küche bei 30 %
"Mach das Licht an" (gesprochen im Büro)      → Büro-Licht an (endpoint-derived)
"Mach das Licht an" (über Signal geschickt)   → "Welcher Raum?"
```

In each case Hermes' session log should show a single trace that ties together the user utterance, the HA-MCP tool call, and the response. `cloud_audit` (in oscar-brain's Postgres) must be empty — local Ollama should handle this without escalation.

## Phase 4 forward

Multi-room voice routing changes the "no-room-said → use endpoint room" rule: if the user has visibly walked into another room (presence detection), the endpoint might lag the user's location. Phase 4 will introduce a presence-aware default. Until then, endpoint-room is the right default.
