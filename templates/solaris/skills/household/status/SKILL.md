---
name: solaris-status
description: Sagt, ob Solaris selbst gerade rundläuft — Gedächtnis, Haussteuerung, Sprachsteuerung. Erreicht die Bewohner als get_solaris_status plus SOUL-Zeiger, nicht als Prompt-Text.
kind: skill
scope: household
version: 3.0.0
author: Solaris
license: MIT
---

# Solaris — läuft alles?

Die Fähigkeit erreicht die Bewohner als **Werkzeug**: `get_solaris_status`
nimmt keine Parameter und meldet `alles_ok` plus die geprüften Teile
(Gedächtnis, Haussteuerung, Sprachsteuerung). Nur lesend, keine
ServiceBay-Reichweite — tiefere Diagnose bleibt beim Betreiberzugang.

Kein Fließtext für den Haushalts-Prompt (#1291): die SOUL trägt nur den
Auslöser→Werkzeug-Zeiger und die Regel, nur die zurückgegebenen Teile zu nennen.
