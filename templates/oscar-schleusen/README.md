# oscar-schleusen

ServiceBay Pod-YAML-Template: ein Container pro Schleuse, alle im selben Pod. Jeder Container ist ein FastMCP-Server mit Shared-Bearer-Auth gegen HERMES.

Welt-Schleusen: Cloud-LLM, Wetter, Websuche (Phase 1) → TuneIn (Phase 4). Anreicherungs-Schleusen: Open Library, MusicBrainz, Discogs (Phase 3a).

Container-Code lebt in [`../../schleusen/<name>/`](../../schleusen/). Bau-Pattern und `variables.json`-Beispiel: [`docs/schleuse-skeleton.md`](../../docs/schleuse-skeleton.md).
