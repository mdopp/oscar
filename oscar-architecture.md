# O.S.C.A.R. — Architektur-Kontext

> Lebendes Dokument. Stand: Mai 2026 (Update nach ServiceBay v3.16+ und Architektur-Umkehr: OSCAR ownt Voice, HA ist MCP-Tool). Erarbeitet im Konzept-Dialog, übergeben an Claude Code zur Umsetzung.

## Vision

O.S.C.A.R. ist ein privates Betriebssystem für Familie und Zuhause: ein lokal laufender, allwissender Assistent, der digitales und physisches Leben orchestriert, als unendliches Gedächtnis dient und absolute Privatsphäre garantiert.

### Fünf Kernziele

1. **Digitale Souveränität (Tresor)** — moderne KI nutzen, ohne Datenpreisgabe. Das Gehirn verlässt das Haus nicht.
2. **Kontextbezogenes Langzeitgedächtnis** — vernetztes Verständnis für Vorlieben, Werte, Erlebnisse, Beziehungen.
3. **Proaktive Orchestrierung** — physische Welt (Haus) verknüpft mit digitaler Welt (Dokumente, Termine, Gewohnheiten).
4. **Eiserne Privatsphäre im Raum** — wahrt Individualität jedes Bewohners, erkennt Gäste, gibt nur frei, was freigegeben ist.
5. **Reibungslose, natürliche Interaktion** — Sprache zuhause, Chat unterwegs, ein zusammenhängendes Erlebnis.

## Architektonische Grundentscheidung: Voice belongs to OSCAR

Im Gegensatz zur HA-zentrierten Voice-Architektur (HA macht STT/TTS, HERMES ist HA-Conversation-Agent) **ownt OSCAR die komplette Voice-Pipeline**:

- HA Voice Preview Edition spricht Wyoming **direkt** mit `oscar-voice` (nicht mehr mit HA)
- Wakeword + STT + Pipeline-Orchestrierung + TTS + Multi-Room-Routing leben im `oscar-voice`-Pod
- HA bleibt der Smart-Home-Hub für Geräte (Z-Wave, Matter, Sensoren, Automations) und exponiert dies via **HA's nativem MCP-Server** als Tool
- HERMES bindet HA-MCP ein wie jeden anderen Tool-Anbieter

**Gewinne**: Identity ohne Header-Marshalling, Voice-Tone/Emotion-Analyse machbar, freie Konversation statt Intent-Grammatik, multimodale Eingaben kombinierbar (Audio + Foto in einem Gemma-Call).

**Voraussetzung**: GPU-Server (kein Mac mini), damit Whisper-large + Gemma 4-12B+ Q4 + Piper-Streaming Latenz < 500ms erreichen.

## Familie & Identitäten

- Drei Personen: Vater (Michael), Mutter, Kind — jedes mit eigenem LLDAP-Account (`uid`)
- Familienmitglieder in LLDAP-Gruppe `family`, Michael zusätzlich in `admins`
- Gäste werden als Gruppe behandelt — Gast-Mode aktiviert sich für jede nicht-erkannte Stimme; kein individueller Gast-LLDAP-Account
- Jede Person hat einen Personal-Harness in `harnesses/{uid}.yaml`
- System soll auch in anderen Haushalten installierbar sein (Multi-Tenant durch eigenen LLDAP + Harness-Repo pro Haushalt)

## Zentrales Konzept: Harness

Begriff im Sinne von Birgitta Böckeler / Martin Fowler:
> "Agent = Model + Harness" — der Harness umfasst alles in einem Agenten außer dem Modell selbst.

### Drei Harness-Typen

| Typ | Aktivierung | Zweck |
|---|---|---|
| **System-Harness** | immer aktiv | Globale Persona, Welt-/Hauswissen, Standard-Tools, Welt-Schleusen-Regeln |
| **Personal-Harness** | bei erkannter Bewohner-Stimme (LLDAP-uid-Match) | Persönlicher Memory-Slice, Vorlieben, erweiterte Tools, höhere Rechte |
| **Gast-Harness** | bei unerkannter Stimme | Nur public Wissen, eingeschränkte Tools, keine Schleusen |

### Fünf Komponenten pro Harness

- **Kontext** — Memory-Namespaces, Vorlieben, Verlaufshistorie
- **Tools** — welche MCP-Werkzeuge dürfen aufgerufen werden
- **Guides** — Antwortstil, Skills, Verhaltens-Anweisungen (Feedforward)
- **Sensoren** — Feedback-Mechanismen, Validatoren (Feedback)
- **Rechte** — Welt-Schleusen-Berechtigungen, Cloud-LLM-Erlaubnis, Ingestion-Rechte

### Beispiel-YAML

```yaml
harness: michael          # entspricht der LLDAP-uid
extends: system
context:
  memory_namespaces: [michael_private, michael_journal, family_shared]
  preferences: { language: de, response_style: concise }
tools:
  inherit_from_system: true
  additional: [finance_docs, personal_email, tax_archive, ingestion]
guides:
  - "Antworten kurz halten, max. 3 Sätze gesprochen"
  - "Bei Finanzdokumenten Quelle nennen"
sensors:
  - thumbs_feedback_via_signal
  - calendar_writeback_confirmation
permissions:
  cloud_llm_schleuse: allowed
  external_search: allowed
  enrichment_schleusen: [open_library, musicbrainz, discogs]
  smart_home: full
```

### Steering Loop

Harnesses werden iterativ verbessert. Wenn O.S.C.A.R. wiederholt etwas falsch macht, ergänzen wir Guides oder Sensors — nicht den Code. Das ist die Wartungsphilosophie.

## Architektur-Schichten

### 1. Eingänge

- **Sprache zuhause**: Home Assistant Voice Preview Edition als Hardware (ESP32 + Mikrofon-Array), aber **Wyoming-Endpoint konfiguriert auf `oscar-voice`** (nicht HA). Zunächst Büro, dann Wohnzimmer, perspektivisch 4–5 Räume.
- **Mobile Chat**: Signal-Bot via HERMES-Gateway (Telegram als Fallback)
- **Wakeword**: Single ("Hey Jarvis" anfangs, später eigenes "Oscar"-Modell), kurze Antworten, "Gast:"-Präfix für unerkannte Stimmen, Beep für erkannte Mitglieder
- **Material-Eingänge**: Foto/Scan/Sprachmemo/Datei in Signal/Telegram **oder** Drop in Syncthing-Inbox-Folder (`/material-inbox/{uid}/`) → Inbound Knowledge Pipeline (siehe Schicht 8)

### 2. Türsteher (Voice-Pipeline + Identitäts-Layer)

`oscar-voice` ist ein Pod mit **vier Containern**: faster-whisper-large-v3, piper, openWakeWord, Türsteher. Türsteher sitzt auf **Rhasspy 3** als Pipeline-Backbone und ist gleichzeitig Identitäts-Layer + Harness-Composer.

#### Pipeline-Verantwortlichkeiten (Türsteher + Rhasspy 3)

- **Wyoming-Server**: empfängt Audio-Streams von HA Voice PE Devices (Port 10300/10200/10400, Standard-Wyoming). Multi-Device-fähig — jedes Voice PE Device adressiert seinen eigenen Session-Kontext.
- **Wakeword-Confirmation-Flow**: openWakeWord triggert, Rhasspy 3 leitet Audio nach Wakeword-Detection an Whisper weiter, Türsteher beobachtet parallel.
- **STT-Anbindung**: `faster-whisper:11300` intern, Streaming-Chunks via Wyoming
- **Speaker-Recognition** (in Phase 2 aktiviert): SpeechBrain ECAPA-TDNN oder Resemblyzer extrahiert ein 256-d Voice-Embedding aus dem Audio-Stream
- **LLDAP-uid-Mapping**: Vergleich des Embeddings mit Türsteher-eigener Postgres-Tabelle (`tuersteher_voice_embeddings` in `oscar-brain.postgres`, FK auf LLDAP `uid`). Voice-Embeddings liegen **nicht** in LLDAP (biometrische PII, binär)
- **Harness-Komposition**: `system.yaml` ∪ (`{uid}.yaml` | `gast.yaml`) → Effective Harness
- **Conversation-Handoff**: ruft HERMES **direkt** auf (HTTP/MCP) mit `(text, uid, audio_features)` — kein Umweg über HA Conversation Agent
- **TTS-Generierung**: bekommt Response-Text von HERMES, ruft `piper:11200` für TTS auf
- **Audio-Rückleitung**: TTS-Audio geht via Wyoming zurück zum originating Voice PE Device
- **Mehrere Personen im Raum**: konservative Schnittmenge der Personal-Harnesses (verfeinerbar in Phase 4)
- **Custom Wakeword "Oscar"**: openWakeWord-Container kann eigene Modelle laden (hostPath-mount) — Phase-4-Feature

#### Was nicht mehr nötig ist

- HERMES-Registration als HA-Conversation-Agent → entfällt
- HA-Pipeline-Konfiguration in `home-assistant/.storage/` → entfällt
- Identity-Header durch HA-Pipeline marshallen → entfällt (Türsteher und HERMES kommunizieren direkt)

### 3. HERMES Agent (Kern)

- Repo: <https://github.com/nousresearch/hermes-agent>
- Provider-agnostisch (Modell-Wechsel via `hermes model`, transparent zwischen lokal und Cloud)
- Eingebaute Gateways: Signal, Telegram, Discord, Slack, WhatsApp, Email (HA-Conversation-Agent-Modus wird **nicht** genutzt)
- **HERMES-eigenes Memory** (Honcho User-Modeling, FTS5 Session-Search, agent-curated Skills) bleibt aktiv — für Konversation und Skill-Memory
- Cron-Scheduler für proaktive Mitteilungen ([`cron/scheduler.py`](https://github.com/NousResearch/hermes-agent/blob/main/cron/scheduler.py), Storage in `~/.hermes/cron/jobs.json`, Skill-Zugriff über `cronjob`-Tool — OSCAR baut keinen eigenen Scheduler, sondern konsumiert diesen)
- Subagent-Spawning für parallele Workflows
- **MCP-Clients**:
  - **ServiceBay-MCP**: Plattform-Operationen (`list_services`, `diagnose`, `get_health_checks`, `start_service`, `restart_service`). Bearer-Token, Scope initial `read+lifecycle`.
  - **HA-MCP** (Home Assistant nativer MCP-Server, Integration `mcp_server` ab HA 2025.x): Geräte-Control (`HassTurnOn`, `HassTurnOff`, `HassSetPosition`, `HassClimateSetTemperature`, `HassMediaPlayer*`), Entity-Liste mit Areas/Aliases. Auth via HA Long-Lived Access Token oder Authelia-OIDC.
  - **OSCAR-eigene MCP-Server**: `oscar-schleusen` (eine pro Schleuse), `oscar-ingestion`, plus Wrapper für Stack-Apps (`immich-search`, `radicale-cal`, `audiobookshelf-list`)

### 4. LLM-Backends

- **Hardware**: GPU-Server (RTX 4070 oder vergleichbar, ≥12 GB VRAM). Kein Mac mini geplant.
- **Standard lokal**: Gemma 4-12B Q4 via Ollama (passt in ~7 GB VRAM, ~30–50 tok/s) — größer als die ursprünglich geplante 4B-Variante, weil GPU es erlaubt
- **Schneller Router** (optional): Gemma 4-1B oder Gemma 4-4B für triviale Befehle ohne LLM-Aufruf
- **Vision/Multimodal**: Gemma 4 ist multimodal für Bild + Text — bedient die Ingestion-Pipeline und kann perspektivisch im Voice-Pfad Audio + Bild kombinieren ("Sieh mal — was ist das?" mit gleichzeitig Kameraschnappschuss)
- **Cloud-Schleuse**: Claude oder Gemini, opt-in pro Anfrage, geht durch die Cloud-LLM-Schleuse mit Audit
- **STT bleibt Whisper**: trotz Multimodal-Gemma — Whisper-large-v3 (auf GPU ~50ms für 3s Audio) ist für reines Transkribieren überlegen und streaming-fähig via Wyoming. Gemma-Audio-Input kann später als zweiter paralleler Pfad für "Audio-Verständnis" (Emotion, Tone) genutzt werden.

### 5. Gedächtnis — zwei Schichten

OSCAR-Memory liegt physisch in **`oscar-brain`** (Qdrant + Postgres laufen als Container im selben Pod wie HERMES + Ollama). Zwei logische Schichten teilen den Pod:

| Schicht | Storage | Zweck | Owner |
|---|---|---|---|
| **HERMES-Konversationsmemory** | Honcho + FTS5 (HERMES-intern) | Dialog-Sessions, Skill-Curation, User-Modeling | HERMES |
| **OSCAR-Domänenmemory** | Qdrant (semantic) + Postgres (strukturiert) | Domain-Collections, Harness-Namespaces | OSCAR-Code |

Harness-uid wird beim Conversation-Call von Türsteher als Request-Parameter mitgegeben; **beide Schichten** respektieren ihn für Memory-Namespace-Filterung.

#### Domänen-Kollektionen (Postgres in `oscar-brain`)

OSCAR führt eigene Tabellen nur dort, wo es **keine ServiceBay-Quelle** gibt:

| Kollektion | Modus | Quelle |
|---|---|---|
| `books` | volle Tabelle | OSCAR-only (keine Buch-App im full-stack) |
| `records` | volle Tabelle | OSCAR-only (Schallplatten — keine ServiceBay-App) |
| `documents` | volle Tabelle | OSCAR-only (bewusst lokal, keine externe Anreicherung) |
| `audiobooks` | **dünne Spiegelung** | Audiobookshelf (ServiceBay `media`-Stack) — OSCAR-Tabelle hält nur Meta-Notizen (Bewertung, Status) + ABS-ID |
| `experiences` | **dünne Spiegelung** | Immich (für Foto-Anker) + Radicale (für Termine) — OSCAR speichert Erlebnis-Notiz + Asset-IDs |

Schema-Felder pro Kollektion (Vector-Index über generierte Beschreibungen, Rückreferenzen auf Original-Material):

- `books` — title, author, isbn, status (`reading|finished|wishlist`), started_at, finished_at, rating, notes, source_image, owner_harness
- `records` — album, artist, year, format (`vinyl|cd`), source_image, owner_harness
- `audiobooks` — abs_id (FK), rating, status_override, notes, owner_harness
- `documents` — type, date, parties, amounts, ocr_text, source_images, tags, owner_harness
- `experiences` — date, type, participants, location, notes, immich_asset_ids, radicale_event_id, owner_harness

Originale (Bilder, Scans) liegen verschlüsselt im **Material-Store** (eigener encrypted Mount), referenziert per UUID.

### 6. Werkzeuge (MCP-Server)

HERMES konsumiert MCP-Tools aus **vier Quellen**:

| Quelle | Bereitstellung | Inhalt |
|---|---|---|
| **ServiceBay-MCP** | `<servicebay-url>/mcp` (nativ in ServiceBay) | Plattform-Operationen: Services, Logs, Diagnose, Backups, Proxy-Routen, Health-Checks |
| **HA-MCP** | HA-Integration `mcp_server` (nativ in Home Assistant) | Geräte-Control, Entity-Liste, Areas, Services, Automations |
| **OSCAR-Stack-App-Wrapper** | Container im `oscar-schleusen`-Pod | `immich-search`, `radicale-cal`, `audiobookshelf-list`, `vaultwarden-read` (eingeschränkt, Audit) |
| **Welt-Schleusen** | Container im `oscar-schleusen`-Pod | TuneIn, Wetter, Websuche, News, Cloud-LLM, Open Library, MusicBrainz, Discogs |

Plus OSCAR-eigene direkte APIs (kein MCP, weil OSCAR-intern):
- Türsteher-Status (welcher Harness aktiv, welches Voice-Device)
- `oscar-ingestion` Material-Pipeline-Trigger
- HERMES Conversation/Memory-API

### 7. Welt-Schleusen

Explizite, regelbasierte Module für jede Außenanbindung. Jede Schleuse: definierter Zweck, was rausgeht, was reinkommt, geloggt.

**Wohnort**: ein gemeinsamer Pod `oscar-schleusen`, ein Container pro Schleuse, jede exponiert ihren eigenen MCP-Server. HERMES + OSCAR-Tools konsumieren sie per MCP-Aufruf.

**Konversation & Information**

- TuneIn / Internet-Radio
- Wetter-API
- Web-Suche (anonymisiert)
- News-Feeds
- Cloud-LLM (Claude/Gemini) — zusätzliches Logging + Permission-Check

**Anreicherungs-Schleusen** (von der Ingestion-Pipeline aufgerufen, opt-in pro Material-Typ)

- Open Library / Google Books — Buchcover, ISBN, Genre, Autor-Bio
- MusicBrainz — Album-Metadaten, Tracks
- Discogs — Schallplatten-Details, Pressungen
- (Dokumente werden bewusst **nicht** angereichert — bleiben strikt lokal)

Alle Aussen-Aufrufe gehen über NPM und werden in AdGuard als bekannte Hosts protokolliert — zweite Audit-Spur neben den Schleuse-eigenen Logs.

### 8. Inbound Knowledge Pipeline (Wissens-Ingestion)

Eingehende Materialien (Foto, Scan, Sprachmemo, Dateianhang) durchlaufen eine eigene Pipeline statt des Konversations-Loops.

#### Trigger

- **HERMES-Gateway**: Signal/Telegram-Nachricht mit Datei/Foto-Anhang → HERMES leitet an `oscar-ingestion` weiter
- **Syncthing-Inbox**: Datei erscheint in `/material-inbox/{lldap-uid}/` (Syncthing-Folder pro Familienmitglied, mit Handy gespiegelt) → `oscar-ingestion`-Watcher detektiert via inotify

Beide Trigger landen im selben Pipeline-Eintrittspunkt.

#### Anwendungsfälle

1. **Sammlungs-Erweiterung**
   - Foto vom Buchcover → Eintrag in `books` mit Status
   - Foto vom Plattencover → Eintrag in `records`
   - Foto vom Audible-Screenshot → Eintrag in `audiobooks` (dünne Spiegelung gegen Audiobookshelf-Match)
   - Optional mit Begleittext oder Sprachnotiz: Bewertung, Quelle, Kontext

2. **Dokumenten-Archivierung**
   - Foto/Scan von Versicherungspolice, Kassenbon, Behördenpost
   - OCR + Klassifikation → Eintrag in `documents`
   - Multi-Page-Scans (mehrere Fotos kurz hintereinander) zu einem Dokument zusammengefasst

3. **Erlebnis-Notizen**
   - Foto vom Konzertticket, Restaurant, Ausflug
   - Eintrag in `experiences` mit Immich-Foto-Anker, optional ins `family_shared`-Memory

#### Vier Pipeline-Stufen

```
Material trifft ein (Signal/Telegram ∪ Syncthing-Inbox)
    ↓
[1] Pre-Processing
    - Original verschlüsselt im Material-Store ablegen
    - OCR auf Text-Bereiche (Tesseract lokal oder Vision-LLM)
    - Multi-Bild-Bündelung über Zeitfenster + Inhalts-Ähnlichkeit
    ↓
[2] Klassifikation
    - Vision-LLM (Gemma 4 multimodal): Buch | Schallplatte | Hörbuch | Dokument | Bon | Erlebnis | unbekannt
    - Metadaten-Extraktion: Titel, Autor, Datum, Betrag, Empfänger
    - Begleittext / Sprachnotiz fließt in Klassifikation ein
    ↓
[3] Anreicherung
    - Buch → Open Library / Google Books (externe Schleuse, opt-in)
    - Musik → MusicBrainz / Discogs (externe Schleuse, opt-in)
    - Hörbuch → Audiobookshelf-Match (interner MCP-Lookup statt externer Schleuse)
    - Dokument → keine externe Anreicherung (lokal)
    - Erlebnis → Immich-Match (interner MCP-Lookup)
    ↓
[4] Bestätigung & Ablage
    - Chat-Dialog: "Ich erkenne X. Soll ich es als Y eintragen? [Ja] [Anpassen] [Nein]"
    - Bei Bestätigung: Eintrag in Domänen-Kollektion (volle Tabelle oder dünne Spiegelung) + Vector-Index + Original-Verweis
    - Bei Anpassen: kurze Korrektur-Konversation
    - Bei Nein: Material verworfen, Bild gelöscht
```

#### Material-Store

- **Eigener encrypted Mount**, *nicht* über den `file-share`-Stack (file-share ist familienöffentlich; Material soll harness-scoped sein)
- RAID-geschützter NAS-Mount
- Pfad-Schema: `/material/{lldap-uid}/{collection}/{uuid}.{ext}`
- Lebenszyklus: nicht-bestätigtes Material wird nach 24 h automatisch gelöscht

## Plattform: ServiceBay v3.16+

- Repo: <https://github.com/mdopp/servicebay>
- Runtime: **Podman Quadlet** (rootless, systemd-integriert) — nicht Docker
- OS: **Fedora CoreOS**, immutable, self-updating
- Template-Format: Kubernetes Pod-Manifeste (`template.yml`) mit Mustache-Variablen, deployed als Quadlet `.kube`-Units
- Variablen-Typen in `variables.json`: `text`, `secret`, `select`, `device`, `subdomain` (mit `proxyPort`, `proxyConfig`, `oidcClient`-Block)
- Multi-Node-Management via SSH (für späteren GPU-Server-Aufrüstung etc.)
- Reactive Digital Twin Architektur (Python-Agent → Backend → UI ohne Polling)
- Diagnose-Probes (crash_loop, cert_expiry, proxy_route_missing, post-deploy-exit, …): Sensorenstrom für Harness-System gratis via MCP `diagnose`-Tool
- **MCP-Server** (`/mcp`): HTTP-Endpoint, Bearer-Token mit Scopes `read|lifecycle|mutate|destroy`, Auto-Snapshot vor destruktiven Calls, Audit-Log

### Was OSCAR aus dem ServiceBay full-stack konsumiert

| Bedarf | Source | Verhältnis |
|---|---|---|
| Smart-Home, Z-Wave, Matter | `home-assistant` | konsumiert via HA-MCP-Server (Integration `mcp_server`), **ohne HA's Voice-Pipeline** |
| Identity, SSO, OIDC | `auth` (LLDAP + Authelia) | direkt |
| Fotos | `immich` | über `immich-search`-MCP-Wrapper |
| CalDAV/CardDAV | `radicale` | über `radicale-cal`-MCP-Wrapper |
| Hörbücher | Audiobookshelf (im `media`-Pod) | über `audiobookshelf-list`-MCP-Wrapper |
| Musik | Navidrome (im `media`-Pod) | Symfonium-Mobile-Client direkt, OSCAR-MCP-Wrapper für Steuerung optional |
| Datei-Drop / -Sync | `file-share` (Syncthing + Samba + FileBrowser + WebDAV) | Syncthing als Material-Eingangs-Trigger |
| Reverse Proxy + LE-Zertifikate | `nginx` (NPM) | für OSCAR-Web-UIs |
| DNS-Sinkhole | `adguard` | Audit-Spur für Schleusen-Außenaufrufe |
| Passwort-Manager | `vaultwarden` | über `vaultwarden-read`-MCP-Wrapper (eingeschränkt) |

→ **OSCAR baut nichts davon nach.** OSCAR konsumiert sie über MCP-Tools oder per Schreibzugriff auf shared volumes.

ServiceBays `voice`-Template (nach mdopp/servicebay#348) ist **für Nicht-OSCAR-Setups** — OSCAR deployt es nicht. `oscar-voice` ersetzt es vollständig.

### OSCAR als External Registry

Der Haushaltsbetreiber trägt unter Settings → Registries `github.com/mdopp/oscar.git` als External Registry ein. ServiceBay klont das Repo nach `~/.servicebay/registries/oscar/` und liest `templates/` + `stacks/` daraus. Die vier OSCAR-Templates erscheinen im Wizard neben den built-in.

### ServiceBay-Patches die OSCAR voraussetzt

- **<https://github.com/mdopp/servicebay/issues/348>** — Wyoming-Stack aus `home-assistant`-Template in eigenes `voice`-Template herausziehen + `VOICE_BUILTIN`-Variable im HA-Template. Ohne diesen Patch laufen Whisper/Piper/openWakeWord zwingend im HA-Pod und kollidieren mit `oscar-voice` auf den gleichen Wyoming-Ports. **Blocker für Phase 0.**
- **HA-MCP-Server-Integration** (`mcp_server`) — gehört zu HA Core ab 2025.x. Im OSCAR-Setup wird er aktiviert und via Authelia-OIDC oder Long-Lived Access Token gegen HERMES authentifiziert.

## Repo-Struktur O.S.C.A.R.

```
github.com/mdopp/oscar/
├── README.md
├── CLAUDE.md
├── docs/
│   ├── architecture.md            # dieses Dokument
│   ├── harness-spec.md            # JSON Schema für Harness-YAMLs
│   ├── ingestion-spec.md          # Schema für Domänen-Kollektionen + Pipeline
│   └── phase-plan.md
├── templates/                     # ServiceBay Pod-YAMLs (Mustache-rendered)
│   ├── oscar-voice/               # Rhasspy-3-Basis + Whisper + Piper + openWakeWord + Türsteher
│   ├── oscar-brain/               # HERMES + Ollama (GPU) + Qdrant + Postgres
│   ├── oscar-schleusen/           # 1 Container pro Schleuse, jeweils eigener MCP-Server
│   └── oscar-ingestion/           # Pipeline + Syncthing-Watcher + Material-Store-Mount
├── stacks/
│   └── oscar/                     # Bundle: voice + brain + schleusen + ingestion
├── tuersteher/                    # Python-Code für Türsteher-Container in oscar-voice
├── ingestion/                     # Python-Code für oscar-ingestion-Container
├── schleusen/                     # Code pro Schleuse — wird in oscar-schleusen-Container gebündelt
│   ├── tunein/
│   ├── wetter/
│   ├── websuche/
│   ├── cloud-llm/
│   ├── open-library/
│   ├── musicbrainz/
│   └── discogs/
├── harnesses/                     # YAML pro LLDAP-uid + system.yaml + gast.yaml
│   ├── system.yaml
│   ├── michael.yaml                # Dateiname = LLDAP-uid
│   ├── anna.yaml
│   ├── kind.yaml
│   └── gast.yaml
└── skills/                        # HERMES-Skills
    ├── morgendliche-nachrichten/
    ├── haus-routinen/
    ├── familien-kalender/
    └── ingest-confirm/
```

## Phasen-Roadmap

### Phase 0 — Voice + Brain Fundament

Ziel: Spürbar bessere Sprachsteuerung als Google Home, vollständig lokal, mit echter Konversation statt Intent-Grammatik.

**Voraussetzungen**:
- **GPU-Server**: RTX 4070 (oder vergleichbar, ≥12 GB VRAM) im PC/Server-Setup
- ServiceBay v3.16+ installiert, full-stack deployed
- **mdopp/servicebay#348 gemerged** (HA-Template kann ohne Wyoming deployed werden)
- HA-Pod redeployed mit `VOICE_BUILTIN=disabled` + HA-MCP-Integration (`mcp_server`) aktiviert

**Lieferungen**:
- ServiceBay OSCAR-Registry eintragen (`github.com/mdopp/oscar.git`)
- **`oscar-voice`-Template schreiben**:
  - Pod-YAML mit faster-whisper-large-v3, piper, openWakeWord, Rhasspy 3, Türsteher
  - Türsteher initial **als Pass-through** (kein Speaker-ID, kein Embedding) — fokussiert auf Pipeline-Orchestrierung und HERMES-Handoff
  - Wyoming-Ports 10300/10200/10400 nach außen (HA Voice PE Devices verbinden hierher)
- **`oscar-brain`-Template schreiben**:
  - Pod-YAML mit HERMES, Ollama (GPU-Passthrough), Qdrant, Postgres
  - Gemma 4-12B Q4 als Default-Modell, Gemma 4-1B als Schnell-Router
  - HERMES erhält Bearer-Token für ServiceBay-MCP (`read+lifecycle`) und HA-MCP (Long-Lived Access Token)
  - Postgres-Backup: wöchentlicher `pg_dump` als CronJob im Pod, dedizierter Volume-Mount für Dumps, 4 Wochen Retention
- HA Voice Preview Edition für Büro bestellen + auf `oscar-voice` konfigurieren
- LLDAP-User für Familie anlegen (Michael, Mutter, Kind), `family`-Gruppe
- Lokale MP3s an Music-Folder von `media`-Pod binden
- Symfonium auf Handy gegen Navidrome konfigurieren
- Erste Skills in HERMES (statt HA Custom Intents): Licht, Heizung, Timer/Wecker (Spec: `docs/skill-zeit.md`), Musik (lokal über Navidrome via HA-Media-Player)

Resultat: Vollständig OSCAR-eigene Voice-Pipeline, HA als Geräte-Tool, eine Identität für alle (noch kein Voice-ID).

### Phase 1 — Mobile + Schleusen

**Signal-Gateway:**
- HERMES-eingebautes Signal-Gateway ([`gateway/platforms/signal.py`](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/signal.py)) aktivieren als **Linked Device** einer bestehenden Familien-Nummer, nicht als eigene OSCAR-Nummer
- `signal-cli-daemon`-Sidecar-Container im `oscar-brain`-Pod (HERMES erwartet HTTP-Endpoint, kein direktes signal-cli-Binary), Env-Vars `SIGNAL_HTTP_URL=http://localhost:8080` + `SIGNAL_ACCOUNT=<Linked-Device-Nummer>`
- Persistent Volume für signal-cli-Session-State (sonst Re-Pairing nach jedem Restart)
- Pairing-Flow per QR-Scan einmal beim Initial-Deploy, in README dokumentieren
- `gateway_identities`-Tabelle in `oscar-brain.postgres` einführen (Spec: `docs/gateway-identities.md`) — Telefonnummer → LLDAP-uid-Mapping
- Admin-Skill `identitaet.verknuepfe` für Erstbefüllung — dünn: HERMES enforced Admin-Permission über Harness, Skill selbst nur E.164-Regex + LLDAP-uid-Existenzcheck + INSERT. Kein Web-UI in Phase 1.
- **Roll-out-Reihenfolge**: Michael zuerst allein, Familie en bloc erst nach ~2 Wochen Stabilitäts-Probe (kein Re-Pairing, keine verlorenen Nachrichten)
- Telegram parallel als zweiter Gateway, gleicher Mechanismus — kein automatischer Failover, symmetrisch zu Signal
- Routing-Endpoint `signal:<phone>` / `telegram:<chat-id>` durchgereicht an Skills wie `zeit` (Spec: `docs/skill-zeit.md`)

**Schleusen:**
- Erste `oscar-schleusen`: Cloud-LLM, Wetter, Websuche (**TuneIn vertagt** — kommt erst mit Music Assistant in Phase 4)
- Schleusen-Skeleton (Python + FastMCP) als Vorlage — Repo-Layout, Tool-Pattern, Auth, variables.json-Beispiel: Spec `docs/schleuse-skeleton.md`
- API-Keys/Secrets über ServiceBay-`variables.json` (`type: secret`), Wizard-Abfrage beim Deploy
- Permission-Enforcement: HERMES prüft Harness-Erlaubnis **vor** dem MCP-Call; Schleusen-Container trusten HERMES (kein Doppel-Check)

**Cloud-LLM:**
- **Automatische Eskalation**, kein Sprachschlüsselwort: Gemma-1B-Router schätzt Komplexität (Token-Budget, Schritt-Tiefe, Kontext-Defizit). Über Schwellwert *und* Harness-Permission → Cloud-LLM-Schleuse. Sonst Gemma-12B lokal.
- Audit-Tabelle `cloud_audit` in `oscar-brain.postgres`: timestamp, uid, trace_id, prompt-hash, prompt-length, response-length, vendor, kosten, **Router-Score + Eskalationsgrund**. Volltext-Mitschreiben hängt am globalen `debug_mode` (siehe Querschnitt-Sektion), nicht an einem eigenen Per-Call-Opt-In.
- Audit-Abfrage per Sprache/Chat („Was hat die Cloud-Schleuse heute weitergegeben?") — kein Web-UI in Phase 1.

Resultat: Konversation unterwegs, Welt-Zugang opt-in pro Harness, automatisches Up-Routing für komplexe Anfragen.

### Phase 2 — Türsteher Speaker-ID + Harnesses

- SpeechBrain ECAPA-TDNN in Türsteher aktivieren
- Voice-Embedding-Tabelle in `oscar-brain.postgres` anlegen, FK auf LLDAP-uid
- Embeddings pro Familienmitglied einlernen (z.B. 10 Sätze pro Person; Setup-Wizard im Türsteher-Web-UI, Authelia-OIDC-geschützt)
- Harness-YAML-Schema formalisieren (JSON Schema in `docs/harness-spec.md`)
- Memory-Namespaces in Qdrant + Postgres einführen
- System + Michael + Gast als erste Harnesses
- Verbal-Hinweise für Gast-Mode

Resultat: Privatsphäre gewahrt.

### Phase 3a — Streaming-Ingestion

- `oscar-ingestion`-Template (Pipeline-Container + Syncthing-Watcher)
- Material-Store als eigener encrypted Mount, 24h-TTL für unbestätigtes Material
- Syncthing-Inbox-Ordner pro LLDAP-uid einrichten (`/material-inbox/{uid}/`)
- Vision-Klassifikator über Gemma 4 multimodal
- Schema-Migrationen für Domain-Collections in Postgres
- Anreicherungs-Schleusen ins `oscar-schleusen` (Open Library, MusicBrainz, Discogs)
- Inkrementeller Roll-out pro Material-Typ:
  1. **Bücher zuerst** — eigene Tabelle, Open Library
  2. **Schallplatten** — eigene Tabelle, MusicBrainz / Discogs
  3. **Hörbücher** — dünne Spiegelung auf Audiobookshelf
  4. **Dokumente** — komplett lokal, OCR-fokus, Steuer-Archiv-Tags
  5. **Erlebnis-Notizen** — dünne Spiegelung auf Immich + Radicale

### Phase 3b — Bulk-Import + MCP-Wrapper

- MCP-Wrapper-Tools im `oscar-schleusen`-Pod (oder eigener `oscar-mcp-wrappers`-Pod):
  - `immich-search` — Foto-Suche (Vision + Metadaten)
  - `radicale-cal` — Termin-CRUD
  - `audiobookshelf-list` — Hörbuch-Bibliothek
- Signal-Verlauf-Import (Familien-Signal-Archive parsen)
- Google Takeout (Maps-Historie, Fotos via Immich)
- Audible-Listen (entweder Audiobookshelf direkt oder Screenshot-Ingestion)
- E-Mail/CalDAV/CardDAV lokal sync (via Radicale)

Resultat: tiefes rückwirkendes Langzeitgedächtnis.

### Phase 4 — Aktive Erweiterungen (laufend)

- HERMES als Mitschreiber (proaktive Memo-Erstellung aus Konversationen)
- Voice-Tone-/Emotion-Analyse als zusätzlicher Türsteher-Sensor (Gemma multimodal auf Audio-Stream parallel zu Whisper-STT)
- Multi-Room-Voice-Routing: mehrere Voice PE Devices, Türsteher route't Antworten zum originating Device
- `oscar-music-assistant`-Template, sobald ≥2 Räume mit Voice (Music Assistant für synchronisierte Wiedergabe)
- TuneIn-/Internet-Radio-Schleuse (vorher zurückgestellt, weil ohne Music Assistant nur Einzel-Raum lohnt)
- Routine „Guten Morgen" als zusammengesetzter HERMES-Skill: HA-MCP-Call (Licht 60%, Heizung +1) + TuneIn-Schleuse (DLF) auf primärem Voice-PE-Lautsprecher
- Verfeinerte Welt-Schleusen
- Anwesenheitserkennung per Telefon
- Multi-Haushalt-Distribution (eigener LLDAP + Harness-Repo pro Haushalt)
- Eigene Antwortstimmen pro Familienmitglied (Piper-Voice-Modell-Mapping)
- Custom Wakeword "Oscar" trainieren (eigenes openWakeWord-Modell in `oscar-voice`-Pod)
- Cross-Modal-Suche: "Zeig mir das Buch, das ich letzten Sommer am See gelesen habe"

## Querschnitt: Debug-Modus

Globaler Schalter in `system.yaml`. Während wir OSCAR aufbauen (Phase 0/1, evtl. 2) ist er **per Default an**; mit dem Übergang in den produktiven Familienbetrieb wechselt der Default zu aus.

```yaml
# system.yaml
debug_mode:
  active: true                # Bauphasen-Default
  verbose_until: null         # NULL = unbegrenzt; sonst Timestamp = TTL
  latency_annotations: false  # Pfad-/Latenz-Annotation in Voice-Antworten, separat schaltbar
```

Wenn `active: true`:
- alle OSCAR-Komponenten loggen Volltexte (Prompts, Responses, Tool-Args, Schleusen-Request-/Response-Bodies) statt nur Metadaten
- Retention-Policies in Audit-Tabellen (`cloud_audit`, künftig `tuersteher_decisions`, `ingestion_classifications`) ausgesetzt — keine Auto-Löschung
- mit `latency_annotations: true` zusätzlich „STT 230ms · Router 80ms → 12B local · 1.4s" als Annotation an Voice-Antworten (sinnvoll auf Admin-UIDs gefiltert, nicht für Familienmitglieder)

Komponenten fragen den Mode pro Log-/Audit-Event neu an (kein Caching > 5 s), damit Ausschalten sofort greift. Admin-Skill `debug.set` schreibt die Felder; Aktivierung per Sprache („Debug-Modus für 4 Stunden an" → setzt `active=true, verbose_until=now()+4h`). Auto-Off durch TTL-Prüfung beim Lesen: `verbose = active AND (verbose_until IS NULL OR now() < verbose_until)`.

Konsequenz: es gibt **keinen** separaten Per-Call-Opt-In für Volltext-Logging in der Cloud-Schleuse oder anderswo — der einzige Schalter ist `debug_mode`. User-facing Permissions („darf Cloud" usw.) bleiben davon unberührt.

## Querschnitt: Logging

Zwei Spuren — **operational** (Container-stdout JSON → journald, gelesen über ServiceBay-MCP `get_container_logs` / `get_service_logs` / `get_podman_logs`) und **Domain-Audit** (Postgres-Tabellen in `oscar-brain`, gelesen über HERMES-Skill `audit.query`). Verbunden über `trace_id` pro Conversation-Turn.

Volle Spec: **[`docs/logging.md`](docs/logging.md)** — Shared-Lib `shared/oscar_logging/`, Retention-Policies pro Audit-Tabelle, Log-Level-Konvention, PII-Behandlung, ServiceBay-MCP-Read-Pfad inkl. Secret-Redaction-Layer.

**Bewusst nicht jetzt:** Loki/Vector oder eigener Log-Aggregator (vor Phase 3+ unnötig). Kein eigenes Log-Web-UI — ServiceBay hat bereits einen Log-Viewer.

## Wichtige Festlegungen

| Punkt | Entscheidung |
|---|---|
| Hardware | GPU-Server (RTX 4070 oder vergleichbar, ≥12 GB VRAM). Kein Mac mini. |
| Identity | LLDAP-uid + -groups (`family`, `admins`) aus ServiceBay `auth`-Pod |
| SSO für OSCAR-Web-UIs | Authelia-OIDC, registriert via `oidcClient`-Block in `variables.json` |
| Reverse Proxy + TLS | NPM (ServiceBay `nginx`-Pod) via Wizard |
| DNS-Block | AdGuard (ServiceBay `adguard`-Pod) |
| Voice-Pipeline-Ownership | **OSCAR ownt komplett** (Rhasspy 3 + Türsteher in `oscar-voice`). HA-Voice-Pipeline wird **nicht** genutzt. |
| HA-Rolle | Geräte-Hub via HA-MCP-Server (Integration `mcp_server`), nicht Voice-Vermittler |
| STT-Modell | faster-whisper-large-v3 auf GPU (~50ms für 3s Audio). Whisper bleibt überlegen gegenüber Gemma-Audio für reines Transkribieren. |
| LLM | Gemma 4-12B Q4 default (passt in ~7 GB VRAM auf GPU) |
| Wakeword | Single ("Hey Jarvis" anfangs, später eigenes "Oscar"-Modell), kurze Antworten, "Gast:"-Präfix, Beep für erkannte Mitglieder |
| Offline-Verhalten | Steuerung, Musik (lokal), Gedächtnis funktionieren. Verloren: Wetter, Streaming, externe Suche, Anreicherungs-Schleusen, Cloud-LLM |
| Cloud-LLM | automatische Eskalation aus Gemma-1B-Router-Komplexitätsschätzung, falls Harness erlaubt. Audit (inkl. `trace_id` + Router-Score) in `cloud_audit`-Tabelle. Volltext-Mitschreiben hängt an `debug_mode`, kein eigener Per-Call-Opt-In. |
| Gateway-Identitäten | `gateway_identities`-Tabelle in `oscar-brain.postgres` (Spec: `docs/gateway-identities.md`); Telefonnummer/Chat-ID → LLDAP-uid. **Nicht** in LLDAP. |
| Debug-Modus | Globaler `system.yaml`-Schalter `debug_mode.active` — Bauphasen-Default an; produktiv aus; TTL-Reaktivierung per Admin-Sprachbefehl. Keine komponenten-eigenen verbose-Flags. |
| Logging | Operational → stdout-JSON → journald, gelesen via ServiceBay-MCP (`get_container_logs` u.a.). Domain-Audit → Postgres-Tabellen in `oscar-brain`, gelesen via HERMES-Skill `audit.query`. `trace_id`-Korrelation. Shared-Lib `shared/oscar_logging/` erzwingt Schema. Spec: `docs/logging.md`. |
| Audit-Backup | Wöchentlicher `pg_dump` als CronJob im `oscar-brain`-Pod, eigener Volume-Mount, 4 Wochen Retention. Off-Site-Backup als spätere Roadmap-Phase. |
| Voice-Embeddings | Türsteher-Postgres-Tabelle in `oscar-brain` mit FK auf LLDAP-uid — *nicht* in LLDAP |
| Memory | Zwei Schichten: HERMES-Honcho (Konversation/Skills) + OSCAR-Qdrant/Postgres (Domain-Collections). Harness-uid als Request-Parameter propagiert (kein Header-Marshalling durch HA mehr). |
| Domain-Collections | Volle Tabellen für `books`/`records`/`documents`; dünne Spiegelung für `audiobooks`/`experiences` |
| Material-Trigger | Signal-Foto ∪ Syncthing-Inbox pro LLDAP-uid |
| Material-Store | eigener encrypted Mount (nicht über `file-share`-Stack) |
| Schleusen | gemeinsamer `oscar-schleusen`-Pod, 1 Container pro Schleuse, jeweils MCP-Server |
| ServiceBay-Steuerung durch HERMES | per ServiceBay-MCP-Endpoint mit Bearer-Token (`read+lifecycle` initial) |
| HA-Steuerung durch HERMES | per HA-MCP-Endpoint mit Long-Lived Access Token oder Authelia-OIDC |
| Mobile Music | Symfonium → Navidrome (ServiceBay `media`-Pod) |
| Mobile Audiobooks | Audiobookshelf-eigene Apps (ServiceBay `media`-Pod) |
| Mobile Chat | Signal → HERMES-Gateway |
| VPN-Zugriff von außen | Wireguard (existiert) |
| Vision-Modell | Gemma 4 multimodal, derselbe Stack wie Text |
| Material-Originale | verschlüsselt im Material-Store, referenziert per UUID |
| Anreicherung Dokumente | bewusst nicht — bleiben strikt lokal |
| Music Assistant | später (Phase 4, sobald ≥2 Räume) |
| Backup-Externalisierung | später, eigene Roadmap-Phase |

## Offene Punkte für Claude Code

1. **mdopp/servicebay#348 verfolgen** — Phase-0-Blocker. Vor `oscar-voice`-Template-Schreibung sicherstellen, dass der Patch gemerged ist.
2. **Rhasspy 3 als Pipeline-Backbone integrieren** — Rhasspy 3 evaluieren (Reifegrad, API), entscheiden ob als Container im `oscar-voice`-Pod oder ob Türsteher den Rhasspy-3-Code direkt importiert. Hook-Point für Speaker-ID-Embedding-Extraktion identifizieren.
3. **`oscar-voice`-Pod-Layout** — Wyoming-Ports nach außen (10300/10200/10400), interner Whisper auf 11300. Mehrere Voice-PE-Devices gleichzeitig handhaben (Session-Routing).
4. **`oscar-brain`-Pod-Layout** — vier Container in einem Pod, GPU-Passthrough für Ollama via Quadlet (`AddDevice=nvidia.com/gpu=all`). Postgres-Initial-Schema-Migrationen sauber aufsetzen (alembic, sqitch, …).
5. **HA-MCP-Server-Aktivierung** — HA-MCP-Integration konfigurieren, Token-Auth gegen HERMES, Tool-Listing-Stabilität testen. Eventuell HA-Areas/Aliases-Naming-Konventionen mit OSCAR-Skill-Erwartungen abgleichen.
6. **Türsteher↔LLDAP-Mapping** — Embedding-Einlern-Wizard (Türsteher-Web-UI mit Authelia-OIDC), CLI-Fallback, evtl. HERMES-Skill "Lerne meine Stimme".
7. **HERMES-Memory ↔ OSCAR-Memory** — Harness-uid-Propagation testen, sicherstellen dass beide Schichten den uid respektieren. HERMES-Konfigurations-Hooks identifizieren.
8. **Material-Store-Verschlüsselung** — LUKS-Container oder Dateisystem-Layer (z.B. gocryptfs)? Schlüssel-Verwaltung (TPM, Passphrase beim Boot?).
9. **MCP-Wrapper-Templates** — gehören `immich-search`/`radicale-cal`/`audiobookshelf-list` in `oscar-schleusen` (semantisch passend: konsumieren externe Quellen mit klarem In/Out) oder in einen eigenen `oscar-mcp-wrappers`-Pod?
10. **Authelia-OIDC-Clients** — welche OSCAR-Services haben eine Web-UI? Initial vermutlich: Türsteher-Admin (Stimm-Einlernen), Ingestion-Bestätigungs-Dashboard, ggf. HERMES-Admin-UI.
11. **Ingestion-Pipeline-Skelett** — Trigger-Disambiguation (Signal vs. Syncthing), Bestätigungs-Dialog-Skill in HERMES.
12. **Domain-Collection-Schemas** — Postgres-DDLs für `books`, `records`, `documents` + dünne-Spiegelung-Tabellen für `audiobooks`/`experiences`. Vector-Index-Strategie (Qdrant-Collection pro Domain-Collection? Eine globale mit Filter?).

## Quellen / Referenzen

- HERMES Agent: <https://github.com/nousresearch/hermes-agent>
- Rhasspy 3: <https://github.com/rhasspy/rhasspy3>
- Wyoming Protocol: <https://github.com/rhasspy/wyoming>
- ServiceBay: <https://github.com/mdopp/servicebay>
- ServiceBay Voice-Split-Issue: <https://github.com/mdopp/servicebay/issues/348>
- Home Assistant MCP Server: <https://www.home-assistant.io/integrations/mcp_server/>
- Harness Engineering (Böckeler): <https://martinfowler.com/articles/harness-engineering.html>
- Anatomy of an Agent Harness (LangChain): verlinkt im Fowler-Artikel
- Gemma 4: <https://deepmind.google/models/gemma/>
- LLDAP: <https://github.com/lldap/lldap>
- Authelia: <https://www.authelia.com/>
- Symfonium: <https://symfonium.app/>
- Audiobookshelf: <https://www.audiobookshelf.org/>
- Immich: <https://immich.app/>
- Radicale: <https://radicale.org/>
- Home Assistant Voice Preview Edition: über Nabu Casa
- Open Library API: <https://openlibrary.org/developers/api>
- MusicBrainz API: <https://musicbrainz.org/doc/MusicBrainz_API>
- Discogs API: <https://www.discogs.com/developers>
- Model Context Protocol (MCP): <https://modelcontextprotocol.io/>
