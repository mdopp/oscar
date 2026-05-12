# Connectors

One connector = one subdirectory = one container image = one FastMCP server. All run inside the `oscar-connectors` pod.

Planned connectors:
- Phase 1: `cloud-llm/`, `weather/`, `web-search/`
- Phase 3a: `open-library/`, `musicbrainz/`, `discogs/`
- Phase 4: `tunein/`

Build pattern, layout, FastMCP auth setup, `variables.json` example: [`../docs/connector-skeleton.md`](../docs/connector-skeleton.md).

`_skeleton/` is the copy template for each new connector — fleshed out with the first concrete connector (weather).
