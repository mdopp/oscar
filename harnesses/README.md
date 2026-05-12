# Harnesses

YAML-Dateien pro LLDAP-`uid` + System-Harness + Gast-Harness. Dateiname == `uid`.

Zur Laufzeit komponiert: `system.yaml` ∪ (`{uid}.yaml` | `gast.yaml`) → effektive Harness pro Conversation-Turn.

Felder: `context`, `tools`, `guides`, `sensors`, `permissions` — Schema-Spec kommt in [`../docs/harness-spec.md`](../docs/harness-spec.md) (Phase 2).

`system.yaml` enthält ab Phase 1 den globalen `debug_mode`-Schalter (siehe [`../oscar-architecture.md`](../oscar-architecture.md) → „Querschnitt: Debug-Modus").
