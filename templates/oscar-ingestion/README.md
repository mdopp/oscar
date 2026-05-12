# oscar-ingestion

ServiceBay Pod-YAML template: ingestion pipeline container + Syncthing watcher.

Triggers: Signal/Telegram photo attachment (via HERMES gateway) or a file in `/material-inbox/{uid}/`. Classification via Gemma 4 multimodal (through HERMES), domain routing into Postgres collections in `oscar-brain`.

Material store: separate encrypted mount, **not** in the `file-share` stack. 24 h TTL for unconfirmed material.

Target phase: 3a. Architecture: [`oscar-architecture.md`](../../oscar-architecture.md) → "8. Inbound knowledge pipeline".
