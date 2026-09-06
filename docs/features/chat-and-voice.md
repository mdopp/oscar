# Chat & Voice assistant

Solaris is a German household assistant you reach two ways — by **voice**
through a Home Assistant Voice PE speaker, and by **chat** in the browser.
Both talk to the same in-process **Solaris Engine** (`solaris-chat`) running a
native agent loop against a local llama.cpp `llama-server` on the GPU.

For the full runtime picture (models, prompt assembly, the facade, latency
numbers) see [`solaris-architecture.md`](../../solaris-architecture.md) §1–§2;
for the design rationale (conversation invariants, prompt budget) see
[`solaris-concept.md`](../solaris-concept.md) §1, §4.

## What it does

- **Talk to it.** Ask questions, control the home, set timers, play music.
  A spoken command answers in ≈0.45 s after you stop speaking.
- **Two chat modes.** Both run `gemma4:e4b`: *Zuhause* (household) is the fast
  hot path with the household soul; *Solaris Gründlich* runs the same model
  with reasoning on for deeper answers. A separate *ServiceBay maintenance*
  persona (admin only) can operate the box.
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
  does STT (whisper on the GPU, ≈0.25 s), calls the engine's
  **Ollama-protocol facade** (`/ollama/api/chat`, conversation agent
  `conversation.solaris` — a compatibility surface for HA, not a dependency
  on Ollama itself), and speaks the answer back through the Kokoro-Martin
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

### Latency baseline — speech end to answer start

`solaris-chat/scripts/bench_voice_latency.py` measures the wait a resident
feels. It replaces the microphone with a file: the box's own Kokoro TTS renders
each of ten commands to a WAV (never played), the WAV goes through the real
Wyoming whisper entity HA's pipeline dials, and the transcript goes to the
household chat backend. It times `audio-stop` to `transcript` (t_stt) and
`transcript` to the first token or tool call (t_ttft); the tool decision is
read, never dispatched, so nothing in the house moves. Its docstring carries the
copy-in command for the `solaris-chat` container.

Box run 2026-09-06, ten commands x ten runs, `gemma4:e4b` on llama-server with
the MTP drafter: **total p50 0.43 s, p95 0.47 s** (t_stt 0.23-0.31 s, t_ttft
0.09-0.23 s per command), turn-1 prefill **7,841 tokens**. Every command sits
well under the 1.3 s mark. This is the reference G-2 measures a new hot-path
tool against (#1128); the Ollama-era numbers in #1120 predate the llama-server
backend (#1318) and are not comparable to it.

### Wake word

The Assist pipeline is wired to a trained single-word **"Solaris"** openWakeWord
model (`templates/solaris/post-deploy.py`: `WAKE_WORD_MODEL = "solaris"`,
`install_wake_word_model`). Wake happens **on-device** — no audio leaves the
speaker before the wake word fires.

## Config / env

Wired by `templates/solaris/post-deploy.py` at install; the engine reads:

| env | purpose |
|---|---|
| `LLAMA_SERVER_URL` | local llama-server endpoint (GPU) — the chat backend (#1318) |
| `OLLAMA_URL` | local Ollama endpoint (GPU) — embeddings + vision ingest only; leaving with #1332 |
| `HASS_URL` / `HASS_TOKEN` | Home Assistant API + long-lived token |
| `SOLARIS_API_KEY` | Bearer for the `/ollama` facade + `/api/chat` |

The household model, `gemma4:e4b` with the MTP drafter, is managed by the
`llama` template and stays resident on the GPU. The panel's Model picker
still offers **"Pull a model into the local Ollama"** — that call still
really pulls into Ollama (`OllamaChat.pull`, `/api/model/pull`) and is
unrelated to the llama-server chat path; it retires along with Ollama once
#1332 lands.
