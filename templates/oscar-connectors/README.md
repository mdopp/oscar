# oscar-connectors

ServiceBay Pod-YAML template: one container per connector, all in the same pod. Each container is a FastMCP server with shared-bearer auth against HERMES.

External connectors: Cloud LLM, weather, web search (Phase 1) → TuneIn (Phase 4). Enrichment connectors: Open Library, MusicBrainz, Discogs (Phase 3a).

Container code lives in [`../../connectors/<name>/`](../../connectors/). Build pattern and `variables.json` example: [`docs/connector-skeleton.md`](../../docs/connector-skeleton.md).
