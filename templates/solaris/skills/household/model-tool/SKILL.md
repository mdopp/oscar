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
tool-actions: model.lease, model.lease.1h, model.lease.2h, model.lease.4h, model.lease.until_morning, model.release
tool-cell-schema: {"id": "id", "title": "title", "badge": "status_text", "meta": ["meta"], "actions": ["model.lease.1h", "model.lease.4h", "model.lease.until_morning", "model.release"]}
tool-action-params: {"model.lease": {"model": "$id", "until": "$until"}, "model.lease.1h": {"model": "$id"}, "model.lease.2h": {"model": "$id"}, "model.lease.4h": {"model": "$id"}, "model.lease.until_morning": {"model": "$id"}, "model.release": {"model": "$id"}}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Modell (`.model`)

**Usage:** `.model` zeigt eine Zeile je Profil — welches Modell gerade geladen
ist, wer es hält und bis wann — und schaltet auf Knopfdruck um.

| Zeile | Was sie bedeutet |
|---|---|
| **Haushalt (Gemma)** | der Normalzustand; die Karte gehört dem Haus |
| **Programmieren (Qwen)** | die ganze Grafikkarte für einen Programmierlauf |
| **Foundry (Gemma 12B)** | ein Foundry-Abend; Solaris antwortet weiter, nur langsamer |

Ein Knopf nimmt die Karte **bis zu einer Zeit**, nicht „bis auf Weiteres":
1 Std, 4 Std oder „bis morgen 07:00". Danach kommt sie von selbst zurück —
niemand muss daran denken. `Freigeben` holt sie sofort zurück.

## Warum das Widget die Zeit hält, nicht das Telefon

Ein Dienst wie foundry oder pi-web hält sein eigenes Fenster und erneuert es aus
dem eigenen Prozess. Ein Telefon kann das nicht: es liegt zwei Sekunden nach dem
Tipp wieder in der Tasche. Also hält die **Engine** das Fenster (`holder:
widget`) und erneuert es bis zur gewählten Endzeit, dann gibt sie es zurück
(#1361 — ein Fenster, das niemand erneuert, endet nach der Karenz statt erst zur
Deadline). Eine automatische Verlängerung gibt es nicht: das gewählte Ende ist
das Ende.

- **Zeilen:** `GET /api/portal/models` (`tool-api-path`) — je Profil `id`,
  `title`, `alias`, `state` (`active` = gerade geladen / `available` /
  `preparing` / `releasing`), `status_text`, `holder`, `expires_at`,
  `remaining_s` und ein fertiger `meta`-Satz („gerade geladen · bis 16:30").
  Denselben Inhalt liefert `GET /napi/portal/models` über den Geräte-Token, den
  Weg, den die Kachel geht.
- **Aktionen:** `model.lease` nimmt ein Fenster (`model`, `until` als Dauer
  `1h`/`2h`/`4h` oder als Zielzeit `morgen 07:00` / `2026-09-09T07:00`, bis
  24 h); `model.release` gibt es zurück. `model` = `household` heißt ebenfalls
  freigeben, damit die Haushaltszeile ein vollwertiger Knopf ist und kein
  Sonderfall.
- **Feste Dauern als eigene Aktions-Ids.** Ein `tool-action-params`-Wert ist ein
  flaches Literal oder ein `$feld` (ADR 0014) — eine RemoteViews-Zeile kann
  keine Auswahlliste zeichnen. Darum sind `model.lease.1h`, `model.lease.4h` und
  `model.lease.until_morning` eigene Aktionen mit fest verdrahteter Dauer;
  `model.lease` mit freiem `until` bleibt für Chat und PWA und erscheint in der
  Kachel gar nicht erst, weil die Zeile kein `until`-Feld liefert.
- **Umschalten:** ein anderes Profil, während eines läuft, gibt erst zurück und
  nimmt dann — die Kachel zeigt dabei `wird freigegeben` und danach
  `wird geladen`. Hält ein **anderer** Dienst die Karte, sagt die Aktion das im
  Klartext und ändert nichts.
