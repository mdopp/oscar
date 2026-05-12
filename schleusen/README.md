# Schleusen-Code

Eine Schleuse = ein Unterverzeichnis = ein Container-Image = ein FastMCP-Server. Alle laufen im `oscar-schleusen`-Pod.

Geplante Schleusen:
- Phase 1: `cloud-llm/`, `wetter/`, `websuche/`
- Phase 3a: `open-library/`, `musicbrainz/`, `discogs/`
- Phase 4: `tunein/`

Bau-Pattern, Layout, FastMCP-Auth-Setup, `variables.json`-Beispiel: [`../docs/schleuse-skeleton.md`](../docs/schleuse-skeleton.md).

`_skeleton/` ist die Kopiervorlage für jede neue Schleuse — wird mit der ersten konkreten Schleuse (Wetter) befüllt.
