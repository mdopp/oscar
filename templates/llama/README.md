# llama.cpp (Household Model Server)

[llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` serving
**one** model — Gemma 4 E4B — for Solaris' household hot path: the voice
turns, the chat, the device commands. It replaced Ollama on that path in
solarisbay#1318.

Everything else stays on the `ollama` template: embeddings
(`nomic-embed-text`), the document/vision ingest, and the model lease a
neighbour service declares over the box's GPU.

## Why a second model server

Speculative decoding. Google publishes a Multi-Token-Prediction drafter for
Gemma 4, llama.cpp can use it, and **Ollama has no draft-model knob at all**.
Box-measured on the same weights, same prompt, same 28 generations
(solarisbay#1317/#1318):

| Server | tok/s | Seconds per finished answer |
|---|---|---|
| Ollama, `gemma4:e4b` | 53.0 | 0.62 s |
| llama-server + MTP drafter | 133.5 | **0.30 s** |

Tool calls were 12/12 on both, German answers complete on both.

## Configuration

- `LLAMA_PORT` — loopback port (default `11435`). No proxy route exists:
  llama-server ships no authentication and its only consumer is the Solaris
  Engine on the same host netns.
- `LLAMA_MODEL_REPO` / `LLAMA_MODEL_FILE` / `LLAMA_DRAFT_FILE` /
  `LLAMA_MMPROJ_FILE` — what post-deploy downloads into
  `${DATA_DIR}/llama/models`. Defaults are ggml-org's Gemma 4 E4B Q4_0
  (4.59 GB), Google's MTP drafter (98.7 MB) and the vision projector
  (560 MB).
- `LLAMA_CONTEXT_LENGTH` — the window the model is loaded at (`32768`).
  Unlike Ollama this is fixed at load, not a per-request hint.
- `LLAMA_DRAFT_N_MAX` — drafted tokens per step (`4`; 43.4% accepted, against
  25% at 8).
- `LLAMA_GPU_PASSTHROUGH` — blank auto-detects a CDI-registered NVIDIA GPU.

Point the solaris template's `LLAMA_SERVER_URL` at
`http://127.0.0.1:<LLAMA_PORT>`; leaving it empty makes the engine fall back
to Ollama's `/api/chat`.

## Three traps, all box-measured

1. **`SecurityLabelDisable=true` is not optional.** With the CDI device but
   without the SELinux relaxation, llama-server logs one passing
   `no usable GPU found`, loads the model into RAM and answers from the CPU.
   Nothing anywhere reads as an error. The `.container` Quadlet post-deploy
   installs carries both lines; `podman kube play` drops the device
   altogether, which is why the fixup exists (#1026).
2. **`--draft-max` no longer exists.** The current image refuses to start on
   it ("the argument has been removed"). The MTP drafter needs
   `--spec-type draft-mtp --spec-draft-model … --spec-draft-ngl 99
   --spec-draft-n-max 4`, and `--spec-type` is mandatory. post-deploy checks
   `/slots` for `"speculative": true` after the start and warns when the
   drafter is not in play — otherwise the server just runs at half speed
   with no error.
3. **Thinking is on unless the request turns it off.** llama.cpp renders the
   chat template with `enable_thinking = true`, overriding the canonical
   Gemma template's own `default(false)`. The server flags
   (`--reasoning-budget 0`, `--reasoning-format none`) do *not* help — the
   second one makes it worse, dumping the raw reasoning trace into the
   visible answer. The switch is per request:
   `"chat_template_kwargs": {"enable_thinking": false}`, which the engine's
   `LlamaServerChat` sends on every household turn. With thinking on, the
   same 28 answers cost 4226 generated tokens instead of 674 and take 4.3x
   longer.

## The GPU lease — handing the whole card to another job

Three models want the one 16 GB card: Solaris' E4B (3.9 GB, always on),
foundry's Gemma 4 26B-A4B (14.1 GB, on request) and the coding run's Qwen 3.8
27B (15.0 GB, on request). The last two don't fit beside the first, so they
take the card outright — on request, at any hour, with no presence check
(solarisbay#1320).

post-deploy installs `${DATA_DIR}/solarisbay/gpu-lease.py` for that:

```
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire foundry
python3 ${DATA_DIR}/solarisbay/gpu-lease.py release
```

`acquire` writes `${DATA_DIR}/solarisbay/gpu_lease.json`, then stops
`ollama`, `solaris-whisper`, `solaris-whisper-batch`, `solaris-tts`,
`solaris-wakeword-trainer` and `llama` — the five units the night
measurements stopped by hand, plus Solaris' own model server. It refuses when
someone else already holds the lease. `release` starts them again, waits for
`/health` (cold E4B is about 38 s), and removes the lease file **last**.

**What the resident gets meanwhile.** The lease file lands on the volume the
chat pod mounts, so the Engine sees it as `/var/lib/solaris/gpu_lease.json`
and answers every turn with one fixed German sentence — "Ich rechne gerade an
einer großen Aufgabe…" — instead of waiting out a timeout against a stopped
server. That is `solaris_chat/gpu_lease.py`; no request leaves the pod while
the lease is held. Voice is off for the duration: `solaris-whisper` and
`solaris-tts` are two of the stopped units.

The lease file's *presence* is the whole signal, deliberately: it is written
before anything stops and removed after the model answers again, so there is
no window where the card is gone and nothing knows it.

The `llama-api` health check goes red for the duration — expected, and the
one thing to watch on the box: nothing may restart `llama.service` behind the
lease's back, or the 26B run meets a second model on the card.

## Storage

`${DATA_DIR}/llama/models` — about 5.2 GB with the defaults. post-deploy
downloads to `<name>.part` and renames on completion, so an interrupted
download never leaves a truncated GGUF that llama-server would crash-loop on.

## Health checks

`/health` returns 200 only once the model and the drafter are loaded, so it
serves as both the readiness and the liveness signal. post-deploy registers
it as the `llama-api` HTTP check (60 s) on top of the auto-created
`service`-type check.
