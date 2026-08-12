# Chat & Voice assistant

Solaris is a German household assistant you reach two ways — by **voice**
through a Home Assistant Voice PE speaker, and by **chat** in the browser.
Both talk to the same in-process **Solaris Engine** (`solaris-chat`) running a
native agent loop against a local Ollama on the GPU.

For the full runtime picture (models, prompt assembly, the facade, latency
numbers) see [`solaris-architecture.md`](../../solaris-architecture.md) §1–§2;
for the design rationale (conversation invariants, prompt budget) see
[`solaris-concept.md`](../solaris-concept.md) §1, §4.

## What it does

- **Talk to it.** Ask questions, control the home, set timers, play music.
  A spoken command answers in ≈1.3 s after you stop speaking.
- **Two chat modes.** *Zuhause* (household) runs the fast `gemma4:e2b` model
  with the household soul; *Solaris Gründlich* runs the thorough `gemma4:12b`
  for deeper answers. A separate *ServiceBay maintenance* persona (admin only)
  can operate the box.
- **Home control.** Backed by Home Assistant — lights, covers, media, sensors.
  Confirm-gated devices (locks, garage/gate covers) always ask before acting.

## How to use it

### Voice

Speak to the Voice PE. Typical German commands:

- „Spiele Musik von <Künstler>" — plays from the Jellyfin library.
- „Stelle einen Timer auf 10 Minuten" — the engine's scheduler rings the
  speaker back when it fires.
- „Wie spät ist es?" — 24-hour time.
- „Öffne das Garagentor" — always confirmed first (never opened directly).

When Solaris expects an answer, its reply ends in a question mark — that is the
cue the Voice PE uses to re-open the microphone for your reply.

### Chat

Open `https://<host>/` (behind Authelia SSO). `/` always opens the chat —
Solaris stays talk-first. Each turn offers 2–4 short quick-reply suggestions
you can tap instead of typing. Switch to *Solaris Gründlich* for a slower,
more thorough answer.

Type `#tag` or `@person` mid-message to group and later re-find a conversation
(autosuggest opens as you type). See
[knowledge-system.md](knowledge-system.md) for how those anchors feed the
knowledge layer.

## How it works (brief)

- The Voice PE speaks only to Home Assistant. HA's **Assist pipeline "Solaris"**
  does STT (whisper on the GPU, ≈0.38 s), calls the engine's
  **Ollama-compatible facade** (`/ollama/api/chat`, conversation agent
  `conversation.solaris`), and speaks the answer back through the Kokoro-Martin
  TTS voice. The engine runs its tool loop server-side; HA never sees the tool
  calls.
- **Model + thinking are chosen per turn** — there is no per-session model
  binding. The household prompt is ≤3k tokens: the soul, the skill markdown,
  and the HA entity registry (`entity_id | name | area`, no live state).
- **Quick-reply chips** (`_suggest_answers`, `engine/client.py`) are chat-only.
  **Voice-continues-on-`?`** is enforced by `_question_pending` / `_as_question`
  in `engine/facade.py`.
- The **voice-gatekeeper** (`voice-gatekeeper/`) is a Wyoming-protocol bridge
  that speaks the same facade for wyoming-satellite hardware.

### Wake word

The Assist pipeline is wired to a trained single-word **"Solaris"** openWakeWord
model (`templates/solaris/post-deploy.py`: `WAKE_WORD_MODEL = "solaris"`,
`install_wake_word_model`). Wake happens **on-device** — no audio leaves the
speaker before the wake word fires.

## Config / env

Wired by `templates/solaris/post-deploy.py` at install; the engine reads:

| env | purpose |
|---|---|
| `OLLAMA_URL` | local Ollama endpoint (GPU) |
| `HASS_URL` / `HASS_TOKEN` | Home Assistant API + long-lived token |
| `SOLARIS_API_KEY` | Bearer for the `/ollama` facade + `/api/chat` |

Models are managed by the `ollama` template: `gemma4:e2b`, `gemma4:12b`,
`nomic-embed-text` stay resident on the GPU (`OLLAMA_MAX_LOADED_MODELS=3`).
