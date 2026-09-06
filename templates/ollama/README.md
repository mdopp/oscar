# Ollama — RETIRED

**This template is retired. It installs nothing and must not be installed.**

Ollama served Solaris' models until September 2026. It was replaced in two
steps and is now gone:

* **The chat model** moved to `templates/llama/` in solarisbay#1318 —
  llama.cpp's `llama-server` with Google's MTP drafter, which halved the wait
  per answer (0.30 s against 0.62 s on the same weights). Ollama has no
  draft-model knob at all, so the speed-up was not available to it.
* **Embeddings and the vision ingest** — the last two jobs — moved in
  solarisbay#1332. `nomic-embed-text` is served by a second, small
  `llama-server --embeddings` instance in the same `llama` template, and photo
  and document descriptions go through the household model's multimodal
  projector, which is already loaded.

There is nothing left here to install. The template body, its variables, its
post-deploy hook and its migrations were removed; this file stays so the
retirement is visible where somebody would go looking for the template.

## What did NOT go away

**The `/ollama` facade on the Solaris Engine stays.** Home Assistant's
conversation integration and the voice-gatekeeper speak the Ollama wire
protocol, and the engine answers it on `http://127.0.0.1:<CHAT_PORT>/ollama`.
That is a protocol, not this service. HA's `ollama` config entry is still the
right entry and the Solaris post-deploy still maintains it — do not remove it.

## If the service is still installed on a box

Nothing depends on it, but nothing removes it either: retiring the template
does not uninstall a running service. Remove it through ServiceBay
(`delete_service ollama`), then check that `ollama-warm.service` — a systemd
user unit this template's post-deploy used to install — is stopped and
disabled.

The model directory (about 20 GB) is left in place deliberately. Deleting it
is an operator decision, not a deploy's.
