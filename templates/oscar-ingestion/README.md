# oscar-ingestion

ServiceBay Pod-YAML-Template: Ingestion-Pipeline-Container + Syncthing-Watcher.

Trigger: Signal-/Telegram-Foto-Anhang (über HERMES-Gateway) oder Datei im `/material-inbox/{uid}/`-Ordner. Klassifikation per Gemma 4 multimodal (über HERMES), Domain-Routing in Postgres-Collections in `oscar-brain`.

Material-Store: separater encrypted Mount, **nicht** im `file-share`-Stack. 24h-TTL für unbestätigtes Material.

Zielphase: 3a. Architektur: [`oscar-architecture.md`](../../oscar-architecture.md) → „8. Inbound Knowledge Pipeline".
