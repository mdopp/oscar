---
name: solaris-model-tool
description: The .model dot-command — welches Modell gerade läuft, und die Grafikkarte für eine gewählte Zeit auf Programmieren oder Foundry umschalten.
kind: tool
scope: household
tool-id: model
tool-label: Modell
command: .model
tool-api-path: /api/portal/models
tool-item-id-field: id
tool-actions: model.set, model.lease, model.release
tool-cell-schema: {"id": "id", "title": "title", "subtitle": "status_text", "meta": ["detail"], "badge": "badge", "actions": ["model.set"]}
tool-action-params: {"model.set": {"profile": "$profile", "hours": "$hours"}}
version: 2.1.0
author: Solaris
license: MIT
---

# Solaris — Modell (`.model`)

**Usage:** `.model` zeigt je Wahl eine Zeile — welches Modell wie lange laufen
soll — und schaltet auf Knopfdruck um.

| Zeile | Was sie bedeutet |
|---|---|
| **Haushalt (freigeben)** | die Karte gehört wieder dem Haus — der Normalzustand |
| **Programmieren · 1 h / 4 h / bis morgen 07:00** | die ganze Grafikkarte für einen Programmierlauf |
| **Foundry · 1 h / 4 h / bis morgen 07:00** | ein Foundry-Abend; Solaris antwortet weiter, nur langsamer |

Ein Tipp nimmt die Karte **bis zu einer Zeit**, nicht „bis auf Weiteres".
Danach kommt sie von selbst zurück — niemand muss daran denken.

Auf einen Blick sagt jede Zeile zweierlei: rechts als fettes Kurzwort, **was
gerade passiert** — `läuft` / `wird geladen` / `wird freigegeben`, und gar
nichts, wenn die Zeile still ist; unter dem Titel **welches Modell und bis
wann** — „Qwen 27B · bis 19:42", „Gemma 4 12B · bis morgen 07:00", beim Haus
„Gemma 4 e4b · Haushalt". Zeilen, die nichts tun, nennen nur ihr Modell
(„Qwen 27B"). Die Endzeit steht als **Uhrzeit**, nicht als Restdauer: „noch 42
Min" muss man erst zur aktuellen Zeit dazurechnen, um zu wissen, wann die Karte
wieder frei ist.

Während eines Wechsels sprechen **zwei** Zeilen: die alte „wird freigegeben",
die neue „wird geladen".

## Warum die Zeile die Wahl ist

Die Kachel löst **genau eine** Aktion je Werkzeug auf (ADR 0014): die erste
deklarierte Id, deren Parameter die Zeile füllen kann, gewinnt — eine zweite Id
aus denselben Feldern ist unerreichbar. Ein Profil mit drei Dauer-Knöpfen wäre
also ein Profil mit dreimal demselben Knopf. Darum ist jede Kombination aus
Profil und Dauer eine eigene Zeile, und die Zeile trägt beides: `profile` und
`hours`. Betitelte Aktionen je Zeile kommen mit #1381 B.

## Warum das Widget die Zeit hält, nicht das Telefon

Ein Dienst wie foundry oder pi-web hält sein eigenes Fenster und erneuert es aus
dem eigenen Prozess. Ein Telefon kann das nicht: es liegt zwei Sekunden nach dem
Tipp wieder in der Tasche. Also hält die **Engine** das Fenster (`holder:
widget`) und erneuert es bis zur gewählten Endzeit, dann gibt sie es zurück
(#1361 — ein Fenster, das niemand erneuert, endet nach der Karenz statt erst zur
Deadline). Eine automatische Verlängerung gibt es nicht: das gewählte Ende ist
das Ende.

- **Zeilen:** `GET /api/portal/models` (`tool-api-path`) — je Wahl `id`,
  `title`, `profile`, `hours`, `alias`, `state` (`active` = gerade geladen /
  `available` / `preparing` / `releasing`), das fertige Kurzwort `badge`
  („läuft" / „wird geladen" / „wird freigegeben" / leer), ein fertig
  formulierter `status_text` („Qwen 27B · bis 19:42") und `detail` (das Modell:
  „Qwen 27B"). `status_text` und `detail` sind **nie beide** gefüllt: die Kachel
  klebt Untertitel und Meta zu **einer** Zeile zusammen, also steht der
  Modellname genau einmal darin. Rohe Zustands- oder Zeitfelder zeigt die Kachel
  nie: sie stellt ein Feld unverändert dar, und weder „active" noch
  „1757336400" ist etwas, das jemand lesen will. Denselben Inhalt liefert
  `GET /napi/portal/models` über den Geräte-Token, den Weg, den die Kachel
  geht.
- **`hours`:** eine Zahl, Brüche eingeschlossen — „bis morgen 07:00" ist um
  Viertel nach sechs abends 12,75 und wird bei **jedem** Abruf neu gerechnet,
  damit die Zeile nicht über den Morgen hinausschießt. `0` heißt freigeben. Die
  Aktion rundet auf ganze Sekunden **auf** und deckelt bei 24 Stunden.
- **Aktionen:** `model.set` (`profile`, `hours`) ist die **einzige** Aktion der
  Kachel. `model.lease` (`model`, `until` als Dauer `1h`/`2h`/`4h` oder als
  Zielzeit `morgen 07:00` / `2026-09-09T07:00`, bis 24 h) und `model.release`
  bleiben für Chat und PWA, wo eine frei gesprochene Zeit möglich ist; im
  `tool-cell-schema` stehen sie nicht, sonst wäre `model.set` unerreichbar.
- **Umschalten:** ein anderes Profil, während eines läuft, gibt erst zurück und
  nimmt dann — dabei tragen zwei Zeilen gleichzeitig ein Kurzwort: die
  abgebende „wird freigegeben", die kommende „wird geladen". Hält ein
  **anderer** Dienst die Karte, sagt die Aktion das im Klartext und ändert
  nichts; die Zeile nennt den Halter („… · von pi-web").
