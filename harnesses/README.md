# Harnesses

YAML files per LLDAP `uid` + system harness + guest harness. Filename == `uid`.

Composed at runtime: `system.yaml` ∪ (`{uid}.yaml` | `guest.yaml`) → effective harness per conversation turn.

Fields: `context`, `tools`, `guides`, `sensors`, `permissions` — schema spec coming in [`../docs/harness-spec.md`](../docs/harness-spec.md) (Phase 2).

`system.yaml` carries the global `debug_mode` switch from Phase 1 onward (see [`../oscar-architecture.md`](../oscar-architecture.md) → "Cross-cutting: debug mode").
