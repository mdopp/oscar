---
name: solaris-status
description: Sagt, ob Solaris selbst gerade rundläuft — Gedächtnis, Haussteuerung, Sprachsteuerung. Nur lesend. Für "läuft alles?"-Fragen.
kind: skill
scope: household
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — läuft alles?

Eine kurze Selbstauskunft: läuft Solaris gerade rund? Nur lesend — es wird nichts
geändert, nichts neu gestartet. In normaler Sprache fragen; ein tippbarer Befehl
ist das nicht.

## Wann

- „Solaris, bist du da?" / „Bist du wach?"
- „Läuft alles?" / „Funktioniert gerade alles?"
- „Ist die Haussteuerung erreichbar?" / „Wo hakt's gerade?"

## Ablauf

1. `get_solaris_status` aufrufen. Das Werkzeug nimmt **keine Parameter** und
   antwortet in dieser Form:
   ```json
   {"alles_ok": true,
    "teile": [{"teil": "Gedächtnis", "ok": true},
              {"teil": "Haussteuerung", "ok": true},
              {"teil": "Sprachsteuerung", "ok": true}]}
   ```
2. Kurz vorlesen:
   - `alles_ok: true` → „Ja, bei mir läuft alles."
   - ein Teil `ok: false` → genau diesen Teil benennen: „Die Haussteuerung
     erreiche ich gerade nicht — Licht und Heizung gehen darum nicht über mich."
   - mehrere → alle nennen, in der Reihenfolge der Antwort.

Nur das vorlesen, was in der Antwort steht. Kein Teil, der dort fehlt, wird
erwähnt oder erraten.

## Was geprüft wird

- **Gedächtnis** — ob Solaris seine eigenen Notizen, Aufgaben und Erinnerungen
  gerade lesen kann.
- **Haussteuerung** — ob Home Assistant antwortet (Licht, Heizung, Rollos).
- **Sprachsteuerung** — ob die Sprachbrücke antwortet.

Ein Teil, den diese Installation nicht hat, steht nicht in der Antwort und wird
auch nicht erwähnt.

## Nicht dafür

- **Einzelne Geräte** („ist das Bürolicht an?") → das ist eine Gerätefrage, kein
  Statuscheck. Dafür gibt es die Home-Assistant-Werkzeuge.
- **Tiefere Diagnose** — Logs, Container, Dienste, Neustarts. Das gehört dem
  Betreiberzugang und ist hier bewusst nicht erreichbar. Wenn ein Teil rot ist
  und jemand mehr wissen will: sagen, dass der Betreiber nachsehen muss.

## Wenn das Werkzeug selbst nicht antwortet

Dann ehrlich sagen: „Das kann ich dir gerade nicht sagen." Niemals einen Zustand
raten und niemals einzelne Sensoren abfragen, um daraus einen Gesamtzustand zu
bauen.
