# Skill `zeit` — Timer & Wecker

> Status: Entwurf, Mai 2026. Zielphase: Phase 0 (Grundfunktion), Erweiterungen ab Phase 1/4. Heimat: `skills/zeit/` (HERMES-Skill).

Ein Skill für **zwei verwandte, aber nicht identische** Use Cases:

| Use Case | Trigger-Semantik | Beispiel-Utterance |
|---|---|---|
| **Timer** | Relative Dauer ab Setzzeitpunkt | „Stell einen Timer auf 5 Minuten", „Pizza-Timer auf 12 Minuten" |
| **Wecker** | Absoluter Zeitpunkt, optional wiederkehrend | „Weck mich morgen um 7", „Werktags um 6:30" |

Geteilte Infrastruktur (Storage, Cron-Trigger, Audio-Routing), getrennte Intents — Anwender denken nie an „Timer um 7 Uhr" oder „Wecker für 5 Minuten".

## Architektur-Verankerung

- **Persistenz**: Tabelle `zeit_jobs` in `oscar-brain.postgres`. Owner-uid, Label, Routing-Endpoint, Kind — alles, was HERMES' eigener Cron nicht weiß.
- **Trigger**: HERMES-Cron-Scheduler ([`cron/scheduler.py`](https://github.com/NousResearch/hermes-agent/blob/main/cron/scheduler.py)) — **bestehende Mechanik nutzen, nicht nachbauen**. `zeit.set` legt zwei Dinge an: (a) eine Zeile in `zeit_jobs`, (b) einen HERMES-Cron-Job via `cronjob`-Tool mit Prompt = „rufe `zeit.fire(<job-id>)` auf". Cancel räumt beides. Storage von HERMES' Cron-Jobs in `~/.hermes/cron/jobs.json` ist Implementations-Detail, das uns nicht stört.
- **Cron-Context-Constraint**: HERMES deaktiviert in Cron-Aufrufen die Toolsets `cronjob`, `messaging`, `clarify` ([Doku](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/cron-troubleshooting.md)). `zeit.fire` darf also keine Nachricht direkt senden — Output geht über die HERMES-Auto-Delivery-Mechanik (`HERMES_CRON_AUTO_DELIVER_PLATFORM`) bei Signal/Telegram-Endpoints, und für `voice-pe:*` über einen Direkt-Aufruf am `oscar-voice`-Service.
- **Ausgabe**: `target_endpoint` ist ein **Routing-Key**, nicht zwingend ein Voice-PE-Gerät — siehe Routing-Endpoints unten. Multi-Room-Fanout erst in Phase 4 ([oscar-architecture.md](../oscar-architecture.md)).
- **Harness-Scoping**: jeder Job trägt `owner_uid`. Listen/Cancel-Intents filtern auf `owner_uid = active_harness_uid`. Im Gast-Mode sieht/manipuliert man nur `owner_uid='gast'`-Jobs.

## Intents

| Intent | Phase | Parameter | Beispiel |
|---|---|---|---|
| `timer.set` | 0 | `duration` (ISO-8601-Duration), `label?` | „Timer 5 Minuten" → `PT5M` |
| `timer.list` | 0 | — | „Welche Timer laufen?" |
| `timer.cancel` | 0 | `label?` oder `id?` | „Stopp den Pizza-Timer" |
| `timer.snooze` | 1 | `duration` | „Noch 5 Minuten" (nach Auslösen) |
| `wecker.set` | 0 | `at` (ISO-8601-DateTime in lokaler TZ) **oder** `rrule` (RFC 5545), `label?` | „Morgen 7 Uhr" → `at=2026-05-12T07:00:00` |
| `wecker.list` | 0 | — | „Welche Wecker sind gestellt?" |
| `wecker.cancel` | 0 | `label?` oder `id?` | „Lösch den Werktags-Wecker" |
| `wecker.snooze` | 1 | `duration` (Default `PT9M`) | „Snooze" (während Klingeln) |

NLU-Parsing der Zeitausdrücke macht die LLM (Gemma 4-12B) — kein eigenes Grammatik-Layer. Der Skill bekommt vom Modell strukturierte Parameter, validiert sie und schreibt sie in die Tabelle.

## Datenmodell

```sql
CREATE TABLE zeit_jobs (
  id              UUID PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('timer','wecker')),
  owner_uid       TEXT NOT NULL,               -- LLDAP-uid oder 'gast'
  label           TEXT,                        -- 'Pizza', 'Werktags-Wecker', NULL
  fires_at        TIMESTAMPTZ NOT NULL,        -- nächster Feuerzeitpunkt (auch bei RRULE)
  rrule           TEXT,                        -- NULL = Einmal-Job; sonst RFC-5545
  duration_set    INTERVAL,                    -- Timer-Originaldauer (für Snooze-Anker)
  target_endpoint TEXT NOT NULL,               -- Routing-Key mit Prefix (siehe Routing-Endpoints)
  hermes_cron_id  TEXT,                        -- Referenz auf HERMES-Cron-Job, ermöglicht Cancel
  state           TEXT NOT NULL CHECK (state IN ('armed','firing','snoozed','done','cancelled')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX zeit_jobs_fires_idx ON zeit_jobs (state, fires_at) WHERE state IN ('armed','snoozed');
CREATE INDEX zeit_jobs_owner_idx ON zeit_jobs (owner_uid, state);
```

### Routing-Endpoints

`target_endpoint` ist die Antwort-Adresse beim Auslösen — derselbe Kanal, von dem der Setzbefehl kam (außer der User sagt explizit etwas anderes).

| Prefix | Beispiel | Setz-Quelle | Fire-Handler |
|---|---|---|---|
| `voice-pe:` | `voice-pe:buero` | Voice-PE-Wyoming-Session | Türsteher → Piper → Wyoming-Stream zurück |
| `signal:` | `signal:+4915112345678` | Signal-Gateway in HERMES | HERMES-Signal-Gateway → Text-Nachricht |
| `telegram:` | `telegram:123456789` | Telegram-Gateway in HERMES | HERMES-Telegram-Gateway → Text-Nachricht |

Der Setzbefehl-Pfad liefert den Wert: Türsteher gibt bei Voice-PE-Calls `voice-pe:<device-name>` an HERMES, HERMES-Gateways setzen bei Signal/Telegram-Calls den entsprechenden Endpoint selbst. Fire-Dispatcher schaut auf Prefix und ruft den richtigen Adapter.

## Lifecycle

```
set        →  state=armed, fires_at gesetzt
fire-tick  →  state=firing, Dispatch an `target_endpoint`
              (voice-pe → TTS „Dein Pizza-Timer ist um"; signal/telegram → Text)
ack        →  RRULE? → state=armed, fires_at = nächstes RRULE-Match
              sonst → state=done
snooze     →  state=snoozed, fires_at = now() + snooze_duration
cancel     →  state=cancelled
```

Fire-Ack ist implizit: erste Sprach-/Knopf-Reaktion nach dem Klingeln. Ohne Ack innerhalb 60 s → erneutes TTS, max. 3 Wiederholungen, dann `state=done` mit Vermerk „unbeantwortet" für späteres `wecker.list`.

## Conversation-Call-Erweiterung

Jeder HERMES-Conversation-Call führt den Routing-Endpoint mit — Türsteher beim Voice-Pfad, HERMES-Gateways beim Chat-Pfad:

```
POST /hermes/converse
{ "text": "…", "uid": "markus", "endpoint": "voice-pe:buero", "audio_features": {…} }
```

`endpoint` ist Phase-0-Pflichtfeld, sobald `zeit` deployed wird. Ohne `endpoint` lehnt HERMES `timer.set` / `wecker.set` ab („Von wo soll ich klingeln?"). Phase 1: Signal-/Telegram-Pfade liefern ihren eigenen Endpoint mit, der Skill funktioniert ohne Voice-PE.

## Phase-Mapping

| Phase | Lieferung |
|---|---|
| **0** | `timer.set/list/cancel`, `wecker.set/list/cancel` mit Einmal- + RRULE-Weckern. Endpoint immer `voice-pe:*`. Ack durch Sprache. |
| **1** | Snooze. `signal:*` / `telegram:*` als Routing-Endpoints, sobald die Gateways live sind — Timer/Wecker von unterwegs setzen und empfangen. |
| **2** | Harness-Filterung scharf (vor Phase 2 ist `owner_uid` immer = einziger Familien-Account). Gast-Mode mit eigenen Jobs. |
| **4** | Multi-Room: `target_endpoint` wird Liste; Wecker können „im ganzen Haus" klingeln. Anhalten von beliebigem Gerät stoppt überall (broadcast-cancel). |

## Festgelegte Entscheidungen

- **Eigener Scheduler, keine HA-Timer.** `zeit` nutzt **nicht** HA-MCP-Services (`timer.start`, `input_datetime`). Begründung: HA ist in OSCAR ausschließlich Haussteuerung; Scheduler/Timer/Wecker leben in `oscar-brain`. Vermeidet doppelten State und macht Multi-Room-Routing (Phase 4) sauber.
- **Erinnerungen sind ein eigener Skill.** „Erinnere mich daran, X zu tun" wird **nicht** in `zeit` gebaut. Stattdessen geplanter Skill `erinnerung` (Phase 1+), der `zeit_jobs` als Scheduling-Backend wiederverwendet, aber den Reminder-Text + Kontext in eigener Tabelle hält. `zeit_jobs.label` ist nur ein Kurz-Tag, kein Inhaltsfeld.
- **Wecker-Sound: TTS-only.** Kein Piper-vorgerenderter Klingelton in Phase 0. Falls später Bedarf besteht, ein paar WAV-Sounds als Asset in `oscar-voice`.
- **Endpoint-Default = Setz-Kanal.** Wer einen Timer/Wecker per Signal setzt, kriegt die Auslöse-Nachricht per Signal zurück — nicht im Haus. Wer per Voice-PE setzt, kriegt Audio am gleichen Gerät. Cross-Channel-Routing nur auf expliziten Wunsch („Wecker im Wohnzimmer um 7"). Vermeidet UX-Überraschung („warum klingelt mein Wohnzimmer, ich war im Café").
