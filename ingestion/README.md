# Ingestion-Pipeline-Code

Python-Container-Code für den Pipeline-Container im `oscar-ingestion`-Pod.

Stufen: Pre-Processing → Klassifikation (Gemma 4 multimodal über HERMES) → Anreicherung (opt-in via Schleuse) → Bestätigungs-Dialog → Persistierung in `oscar-brain.postgres`-Domain-Collection.

Zielphase: 3a, inkrementeller Roll-out pro Material-Typ (Bücher → Schallplatten → Hörbücher → Dokumente → Erlebnis-Notizen).

Architektur: [`../oscar-architecture.md`](../oscar-architecture.md) → „8. Inbound Knowledge Pipeline".
