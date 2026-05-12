# Gateway-Identities

> Status: Entwurf, Mai 2026. Zielphase: Phase 1 (erste Nutzung mit Signal). Heimat: Tabelle in `oscar-brain.postgres`, geschrieben von Setup-Wizard, gelesen von HERMES vor jedem Gateway-Conversation-Call.

## Zweck

HERMES-Gateways (Signal, Telegram, Email, Discord, …) sehen eingehende Nachrichten mit der **externen Identität** des Absenders — z.B. `+4915112345678` bei Signal, `123456789` bei Telegram. Damit Harness-Auswahl, Memory-Namespace und Skill-Permissions funktionieren, braucht HERMES daraus den **LLDAP-uid**. Diese Tabelle ist das Mapping.

Voice-Erkennung läuft **nicht** über diese Tabelle — Sprach-Embeddings sind kontinuierlich und brauchen eine eigene Distanz-basierte Auflösung in `tuersteher_voice_embeddings` ([oscar-architecture.md:468](../oscar-architecture.md#L468)). Hier nur die exakt-matchbaren Kanäle.

## Datenmodell

```sql
CREATE TABLE gateway_identities (
  gateway      TEXT NOT NULL,                  -- 'signal' | 'telegram' | 'email' | 'discord' | …
  external_id  TEXT NOT NULL,                  -- E.164-Nummer, Chat-ID, E-Mail-Adresse, Discord-ID
  uid          TEXT NOT NULL,                  -- LLDAP-uid (oder 'gast' wird *nicht* hier gespeichert)
  display_name TEXT,                           -- optional, für Logs/UI
  verified_at  TIMESTAMPTZ NOT NULL,
  created_by   TEXT NOT NULL,                  -- LLDAP-uid des Admins, der die Zuordnung gemacht hat
  PRIMARY KEY (gateway, external_id)
);
CREATE INDEX gateway_identities_uid_idx ON gateway_identities (uid);
```

**Mehrere External-IDs pro uid** sind erlaubt (Markus hat private + Arbeits-Signal-Nummer): Primary Key ist `(gateway, external_id)`, nicht `uid`.

**Unbekannte external_id** bedeutet: kein Eintrag, kein Match. HERMES behandelt den Anrufer dann als **Gast-Harness**, lehnt aber per Default sensible Tools ab und antwortet mit Verbal-Hinweis („Ich kenne dich noch nicht — wenn du in der Familie bist, lass dich von Markus eintragen.").

## Lookup-Pfad

```
Gateway empfängt Nachricht
  → HERMES holt (gateway, external_id) → uid via gateway_identities
  → uid NULL? → uid = 'gast'
  → Harness laden = system.yaml ∪ ({uid}.yaml | gast.yaml)
  → Conversation-Call mit (text, uid, endpoint, …)
```

Bei jedem Call frisch nachschlagen — kein Caching im Gateway-Code. Tabelle ist klein, Postgres-Lookup vernachlässigbar gegen LLM-Inference-Zeit.

## Schreibpfad

**Phase 1:** kein Web-UI. Eintrag per HERMES-Skill `identitaet.verknuepfe`, Aufruf nur durch Admin-Harness (`groups: [admins]` in LLDAP):

```
Markus (Voice-PE-Büro, Admin):
  „Verknüpfe Signal-Nummer +49 151 1234 5678 mit dem User anna."
  → Skill prüft Admin-Permission, schreibt Zeile, bestätigt verbal.
```

Verifikation in Phase 1 ist **implizit-vertraulich** — der Admin spricht, der Skill schreibt. Keine Cross-Channel-Bestätigung (z.B. „Sende JA an dieses Signal-Konto").

**Phase 2+:** wenn der Türsteher ein Web-UI für Voice-Embedding-Onboarding bekommt ([oscar-architecture.md:403](../oscar-architecture.md#L403)), ergänzt es einen Reiter „Gateway-Verknüpfungen" — Authelia-OIDC-geschützt, dort manuell editierbar.

## Datenschutz

- Telefonnummern und Chat-IDs sind PII. Tabelle liegt in `oscar-brain.postgres`, **nicht** in LLDAP — analog zur Begründung bei Voice-Embeddings ([oscar-architecture.md:468](../oscar-architecture.md#L468)): biometrische und kontaktbezogene Daten gehören in eine OSCAR-eigene Schicht, nicht in den Identity-Provider.
- Kein HMAC/Hashing: Lookup ist exakt-Match, plain reicht. Postgres ist eh encrypted-at-rest auf Fedora-CoreOS-LUKS-Volumes.
- LLDAP-uid-Löschung → Cascade: bei uid-Delete in LLDAP muss ein Hook (oder periodischer Reconciler in HERMES) verwaiste `gateway_identities`-Zeilen entfernen. Variante 1 (Hook) hängt an ServiceBay; Variante 2 (Reconciler) ist OSCAR-intern und damit robuster gegen ServiceBay-Upgrades. **Empfehlung: Reconciler, täglich.**

## Offene Fragen

1. **Cross-Channel-Verifikation** vor Phase-2-UI: lohnt es sich, schon in Phase 1 einen Bestätigungs-Code-Roundtrip einzubauen („Ich habe dir gerade eine 6-stellige Nummer per Signal geschickt — sag sie laut")? Vorteil: kein Vertrauensbruch durch versehentliche Falschverknüpfung. Nachteil: Komplexität für ein 4-Personen-Setup. Empfehlung: nein, später.
2. **Gast-Konten mit eigener Identität**: wenn ein bekannter Gast (z.B. Schwester) regelmäßig per Signal schreibt — wollen wir einen eigenen `gast:schwester`-uid oder bleibt sie bei `gast`? Hängt vom Harness-Design ab; vertagen bis Phase 2.
