---
name: solaris-media
description: Play and control household media — Jellyfin music, internet radio, podcasts — on a room speaker. Delivered to residents as tools (play_music, play_radio, media_find_podcast) plus the SOUL pointer, not as prompt text.
kind: skill
scope: household
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Medien

Die Fähigkeit erreicht die Bewohner als **Werkzeug**: `play_music`
(Jellyfin-Bibliothek), `play_radio` (Sender/Stream), `media_find_podcast`
(neueste Folge) und die Transportbefehle über `ha_call_service` auf demselben
`media_player`. Raumauflösung, Titelsuche und Fehlerfälle stecken in diesen
Werkzeugen.

Kein Fließtext für den Haushalts-Prompt (#1291): auf dem kleinen
Haushaltsmodell ist die deterministische Werkzeug-Steuerung die eine Quelle der
Wahrheit pro Ablauf, die SOUL trägt nur den Auslöser→Werkzeug-Zeiger.
