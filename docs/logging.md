# Logging-Spec

> Status: Entwurf, Mai 2026. Zielphase: Phase 0/1 (gilt ab erstem Container). Cross-cutting; berührt alle OSCAR-Komponenten.

## Zwei Spuren

| Spur | Wohin | Wofür | Lesen |
|---|---|---|---|
| **Operational** | Container-stdout als strukturiertes JSON → Podman/Quadlet → journald | Errors, Lifecycle, Latenzen, Stack Traces — „was ist umgefallen, wann, in welcher Reihenfolge" | ServiceBay-MCP `get_container_logs(id, since)` / `get_service_logs(name)` / `get_podman_logs` |
| **Domain-Audit** | Postgres-Tabellen in `oscar-brain` (`cloud_audit`, später `tuersteher_decisions`, `ingestion_classifications`, …) | Strukturierte Nachvollziehbarkeit; Retention-Policy; cross-call-Aggregation | HERMES-Skill `audit.query(stream, since, uid, event, …)` |

Verbindendes Element: `trace_id` (UUID pro Conversation-Turn, generiert vom Türsteher beim Eintreffen der Voice-Audio bzw. vom Gateway beim Eintreffen der Signal-/Telegram-Nachricht). Propagiert durch jeden MCP-Call und in jeder Log-/Audit-Zeile mitgeschrieben. Im Debug-Modus die Voraussetzung, um STT-Latenz einem zwei Skill-Schritte später erfolgten Cloud-Call zuordnen zu können.

## Shared-Lib statt Schema-Doku (L1)

Python-Modul `shared/oscar_logging/` im Repo, das jeder OSCAR-Container importiert. Erzwingt das Schema beim Schreiben, statt es per README zu beten:

```python
from oscar_logging import log

log.info("router.decision",
         trace_id=ctx.trace_id, uid=ctx.uid,
         decision="cloud", router_score=0.83, reason="multi-step-plan")
```

Standardfelder werden vom Lib-Aufruf gefüllt: `ts`, `level`, `component` (aus Env-Var `OSCAR_COMPONENT`), `event` (1. Positional), Body als Keywords. Output ist eine JSON-Zeile pro Aufruf auf stdout. Kein direktes `print()` / `logging.info(...)` mehr in Komponenten-Code.

Begründung: fünf Container, in denen alle das gleiche Feld „trace_id" heißen müssen, sind eine README-Folter. Lib mit `log.event(...)` ist robuster gegen Drift.

## Log-Levels (L5)

| Level | Wann | im Default-Modus | im Debug-Modus |
|---|---|---|---|
| `error` | Failure — Operation konnte nicht abgeschlossen werden | immer | immer |
| `warn` | Ungewöhnlich, aber kein Failure (Cloud-Schleuse-Fallback, hohe STT-Confidence-Schwankung) | immer | immer |
| `info` | Normaler Lifecycle, Skill-Aufrufe, MCP-Calls (ohne Bodies) | Default | immer |
| `debug` | Bodies, Zwischen-Scores, Tool-Args, Schleusen-Request-/Response-Inhalte | aus | an |

`debug_mode.active=true` ([oscar-architecture.md Querschnitt: Debug-Modus](../oscar-architecture.md)) öffnet `debug`-Level. Komponenten fragen `debug_mode` pro Log-Aufruf neu an (kein Caching > 5 s).

## Retention der Domain-Audit-Tabellen (L2)

| Tabelle | Phase | Retention (`debug_mode=false`) | `debug_mode=true` |
|---|---|---|---|
| `cloud_audit` | 1 | **90 Tage** | unbegrenzt |
| `tuersteher_decisions` | 2+ | **30 Tage** | unbegrenzt |
| `ingestion_classifications` | 3a+ | **180 Tage** (selten geschrieben, langer Forensik-Wert bei Fehlklassifikation) | unbegrenzt |

Pruning: nightly Postgres-Job (Cron im `oscar-brain`-Pod). Im Debug-Modus übersprungen.

## Reading-Interface (L3)

**Operational-Logs** werden nicht über OSCAR gelesen, sondern direkt über ServiceBay-MCP. HERMES braucht keinen eigenen Skill dafür — wenn der User „warum war OSCAR neulich langsam?" fragt, ruft HERMES `get_container_logs` mit passendem Filter auf.

**Domain-Audit** über *einen* generischen HERMES-Skill:

```
audit.query(
  stream: 'cloud_audit' | 'tuersteher_decisions' | 'ingestion_classifications',
  since: ISO-8601-DateTime | null,
  uid: str | null,
  event: str | null,
  limit: int = 50
)
```

Comfort-Phrasen wie „Was ging heute an die Cloud?" werden vom LLM auf Filter abgebildet (`stream='cloud_audit', since=today-00:00`). Kein eigener Mini-Skill pro Stream — sonst wuchern Skills, die alle dasselbe Pattern haben.

## PII im Metadaten-Modus (L4)

`cloud_audit` (und analoge Tabellen) speichern bei `debug_mode=false` **keinen Volltext**, sondern:

- `prompt_hash` — SHA-256 über den Volltext-Prompt. Erlaubt „identisch zu Anfrage vorhin" als Indikator im Verlauf; kein Reverse.
- `prompt_length`, `response_length` — Tokens, nicht Zeichen
- `vendor`, `cost`, `latency_ms`, `router_score`, `escalation_reason`, `uid`, `trace_id`, `ts`

**Kein Snippet** (erste-50-Zeichen des Prompts). Halb-PII, halb-nutzlos: liefert nicht genug, um die Anfrage zu debuggen, aber genug, um versehentlich private Inhalte aufzubewahren. Wer den Volltext debuggen will, schaltet `debug_mode` an — das ist der saubere Pfad.

## ServiceBay-Integration (L6)

Operational-Logs werden über ServiceBay-MCP gezogen — drei `read`-Scope-Tools sind verfügbar:

- **`get_container_logs(id, since)`** — primärer Pfad. Tail-Pattern mit `since` (Unix-Sekunden) für Debug-Loops; bei jeder Folge-Iteration nur neue Zeilen.
- **`get_service_logs(name)`** — auf Service-Ebene (= ServiceBay-Stack-Service-Name, nicht Container-ID)
- **`get_podman_logs`** — generischer Podman-Tail, für Container ohne ServiceBay-Service-Mapping

HERMES bekommt initial `read+lifecycle`-Bearer ([oscar-architecture.md:129](../oscar-architecture.md#L129)) — alle drei Read-Tools damit zugänglich, kein Scope-Upgrade nötig.

**Secret-Redaction durch ServiceBay:** [`src/lib/mcp/redact.ts`](https://github.com/mdopp/servicebay/blob/main/src/lib/mcp/redact.ts) entfernt bekannte Secret-Patterns (Passwörter, API-Keys, OIDC-Client-Secrets) bevor Logs den MCP-Client erreichen. Eigene Stdout-Scrub-Logik in OSCAR-Komponenten ist überflüssig, solange ServiceBay-MCP der einzige Lese-Pfad ist. Direkter Host-`journalctl` umgeht diese Schicht — nur für Notfälle.

**ServiceBay-MCP-Audit als dritte Spur:** ServiceBay protokolliert jeden MCP-Call serverseitig in `mcp-audit.log`. Hilft beim „warum hat HERMES neulich den Container neu gestartet?", ohne dass OSCAR sich selbst auditen muss.

## Bewusst nicht jetzt

- **Loki / Vector / Promtail.** Kein eigener Log-Aggregator in Phase 0/1. Erst wenn cross-container-Zeitkorrelation über ServiceBay-MCP zu mühsam wird (vermutlich Phase 3+ mit Ingestion-Trails).
- **Web-UI für Logs in OSCAR.** ServiceBay hat eine Log-Viewer-Komponente ([`src/components/LogViewer.tsx`](https://github.com/mdopp/servicebay/blob/main/src/components/LogViewer.tsx)); reicht.
- **Per-Komponenten-`verbose`-Flags.** Es gibt nur den globalen `debug_mode` (Querschnitt: Debug-Modus).
