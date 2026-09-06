# llama.cpp (Household Model Server)

[llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` serving
Solaris' models: **Gemma 4 E4B** for the household hot path — the voice turns,
the chat, the device commands, and (through the multimodal projector) the
photo and document descriptions — plus a second, small instance serving
**`nomic-embed-text`** for the vault's semantic search.

It replaced Ollama on the chat path in solarisbay#1318 and took over the last
two jobs — embeddings and the vision ingest — in solarisbay#1332. The `ollama`
template is retired; nothing on this box runs it any more.

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

- `LLAMA_PORT` — the port llama-server binds (default `11435`). No proxy route
  exists: llama-server ships no authentication, so the endpoint is on-box only.
  See *Who may reach the endpoint* below.
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
- `LLAMA_EMBED_PORT` / `LLAMA_EMBED_REPO` / `LLAMA_EMBED_FILE` /
  `LLAMA_EMBED_ALIAS` / `LLAMA_EMBED_CONTEXT_LENGTH` — the embeddings server
  (below). Empty `LLAMA_EMBED_PORT` skips it.

Point the solaris template's `LLAMA_SERVER_URL` at
`http://127.0.0.1:<LLAMA_PORT>` and its `LLAMA_EMBED_URL` at
`http://127.0.0.1:<LLAMA_EMBED_PORT>`.

## The embeddings server (solarisbay#1332)

A second `llama-server`, its own Quadlet (`llama-embed.service`), loopback
only, ~300 MB of VRAM: `nomic-embed-text-v1.5` f16 with `--embeddings`,
serving OpenAI `/v1/embeddings` on `LLAMA_EMBED_PORT` (default `11436`). The
Solaris Engine embeds the OKF vector store and every semantic vault query
through it.

It is a separate unit rather than a second container in the pod because the
GPU fixup below replaces the pod's `.kube` unit outright, and a pod sibling
would go with it.

**Vector compatibility is the whole point of the defaults.** The rows already
in `okf_vectors` were computed by Ollama's `nomic-embed-text` tag: v1.5, f16,
768 dimensions, mean pooling, on the **raw text** — Ollama never applied the
`search_document:` / `search_query:` prefixes the model card describes, and
neither does Solaris. Same model, same quantisation, same pooling, same raw
text ⇒ old and new vectors are comparable and nothing had to be re-embedded.
Changing `LLAMA_EMBED_FILE` to a smaller quant, or adding a prefix, would not
fail — it would quietly make search worse.

Two flags in the unit are not tuning: `--pooling mean` (the model is
mean-pooled; anything else yields valid-looking, incomparable vectors) and
`--ubatch-size` equal to the context length (an embedding model runs
non-causal attention, and llama.cpp rejects anything longer than one
micro-batch).

## Who may reach the endpoint — an ADR-0007 carve-out for on-box consumers

llama-server has no authentication, so the rule is *on-box only, never the LAN*.
That is three different addresses, and each consumer gets exactly one:

| Consumer | Address | Why |
|---|---|---|
| Services on host networking — the Solaris Engine, post-deploy, the health check | `http://127.0.0.1:11435` | same netns; the default and the fast path |
| Isolated pods without host networking — claude-dev, its `pi` | `http://host.containers.internal:11435` | ADR-0007 Decision 1: never `127.0.0.1`, never the LAN IP |
| Anything on the LAN | *refused* | nothing outside the box may talk to an unauthenticated model server |

The server therefore binds **`0.0.0.0`, not loopback** (#1344). A loopback bind
looks like the safe choice and is not reachable from a sibling pod at all:
rootless podman/pasta maps `host.containers.internal` (`169.254.1.2` here) to
the host's **LAN address**, not to `127.0.0.1`, so `pi`'s model picker came up
empty against a loopback-bound server. Binding the pasta-mapped address instead
would hard-code a LAN IP and take `127.0.0.1` away from the Engine — both
forbidden — and `llama-server` accepts only one `--host`.

The LAN half is closed one layer down instead, outside the pod: `LLAMA_PORT`
carries **`blockLanAccess: true`** in `variables.json`, and ServiceBay renders a
host nftables rule that drops connections to the port arriving on a physical
interface while accepting the ones arriving on `lo` — which is where the
pasta-proxied pod path lands, because pasta re-opens the connection to one of
the host's own addresses and the kernel routes that over loopback. This is the
same pattern LLDAP's raw LDAP port uses (servicebay#2388), and it is what
ADR-0007's Decision 3 prescribes: *the sibling binds wider and carries
`blockLanAccess`; the consumer stays isolated.*

Checking it on the box is two commands — from inside another pod
`curl http://host.containers.internal:11435/v1/models` must answer, and from a
LAN host `curl http://<box-lan-ip>:11435/v1/models` must be refused.

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

Several models want the one 16 GB card, and only Solaris' E4B (3.9 GB) is
always on. A job that needs more asks for it — at any hour, with no presence
check (solarisbay#1320) — and gets one of three shapes: the card emptied
outright, or llama-server swapped onto the coding model (`--model coding`,
solarisbay#1319) or onto foundry's 12B (`--model foundry`, solarisbay#1325).

post-deploy installs `${DATA_DIR}/solarisbay/gpu-lease.py` for that:

```
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire someone
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire coder --model coding --duration 4h
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire foundry --model foundry --duration 5h
python3 ${DATA_DIR}/solarisbay/gpu-lease.py release
```

`acquire` writes `${DATA_DIR}/solarisbay/gpu_lease.json`; without `--model` it
then stops `llama-embed`, `solaris-whisper`, `solaris-whisper-batch`, `solaris-tts`,
`solaris-wakeword-trainer` and `llama` — the five units the night
measurements stopped by hand, plus Solaris' own model server. It refuses when
someone else already holds the lease. `release` starts them again, waits for
`/health` (cold E4B is about 38 s), and removes the lease file **last**.

**Every lease expires.** `--duration` defaults to 4 h and arms a transient
systemd timer (`solaris-gpu-lease-expiry`) that runs `release` at the
deadline. An end signal alone was not enough in solarisbay#1260: a run that
dies without releasing would otherwise leave the household muted, or on the
coding model, until somebody noticed.

**What the resident gets meanwhile.** The lease file lands on the volume the
chat pod mounts, so the Engine sees it as `/var/lib/solaris/gpu_lease.json`
and answers every turn with one fixed German sentence — "Ich rechne gerade an
einer großen Aufgabe…" — instead of waiting out a timeout against a stopped
server. That is `solaris_chat/gpu_lease.py`; no request leaves the pod while
the lease is held. Voice is off for the duration: `solaris-whisper` and
`solaris-tts` are two of the stopped units.

### `--model coding` — the coding window (solarisbay#1319)

The coding run is the exception: instead of emptying the card it **swaps**
what is on it, so the household keeps an assistant.

* llama-server is reloaded on Qwen 3.8 27B `UD-IQ3_XXS` + its MTP drafter,
  `-c 65536 -ctk q8_0 -ctv q8_0 --parallel 1`. Those last two are not tuning:
  with llama-server's stock four slots, or f16 KV, the drafter OOMs before it
  loads. Box-measured 15 004 of 16 380 MiB, 32.6 tok/s, tool calls 12/12
  (solarisbay#1318, cell H1). The 12.6 GB of weights are fetched **before**
  anything stops.
* Solaris answers household turns from that model for the window (mode B) and
  the chat carries a banner naming the model and the end time. Only the swap
  itself is mute — the lease says `ready: false` until `/health` answers.
* `solaris-whisper` and `solaris-tts` keep running, on the **CPU**: operator
  decision of 2026-09-05, spoken commands stay possible and get slower rather
  than disappearing. Both units read their provider from
  `${DATA_DIR}/solarisbay/voice-device.env`, which the lease flips to
  `cpu`/`cuda` and restarts them on; whisper drops to `small-int8` with it.
  `llama-embed`, `solaris-whisper-batch` and `solaris-wakeword-trainer` still
  stop — they hold VRAM and nobody is waiting on them. Semantic vault search
  falls back to keyword hits for the window.

### `--model foundry` — the foundry evening (solarisbay#1325)

foundry writes up a session as it runs and transcribes through
`solaris-whisper-batch` every five minutes, so the one thing it must not do is
take the voice stack away. This profile therefore stops **nothing**:

* llama-server is reloaded on Gemma 4 12B `Q4_0` + its MTP drafter, `-c 32768`,
  f16 KV, llama-server's stock four slots — box-measured 9 626 MiB, 36.6 tok/s,
  1.53 s per finished answer, tool calls 6/6, no thinking leak (solarisbay#1318,
  cell K2). The 12B runs **instead of** the household E4B, not beside it:
  9 636 + 3 872 + the voice stack's 4 508 MiB under load is 18 016 of a
  16 380 MiB card. Without the E4B it is 14 144, with 2 236 MiB to spare.
* All five units — `llama-embed`, `solaris-whisper`, `solaris-whisper-batch`,
  `solaris-tts`, `solaris-wakeword-trainer` — keep running, on the **GPU**;
  `voice-device.env` is not touched. The embeddings server's 300 MB fits
  beside the 12B, so the household keeps its semantic vault search for the
  whole evening.
* Solaris answers the household from the 12B and **shows no banner**: operator
  decision of 2026-09-05. Nothing the resident does changes — voice included —
  except that an answer takes about a second longer. `/api/whoami` still names
  the window under `gpu_lease` for the log. Only the swap itself is mute
  (`ready: false` until `/health` answers).
* No vision projector: the 12B repo's `mmproj` has never been fetched or
  measured on this box, and a file that turned out not to exist would refuse
  the lease outright. A photo attachment reaches the 12B as text for the
  window.

A deploy in the middle of a lease leaves `llama.service` and the voice device
exactly as the lease set them; post-deploy says so in its log and does nothing
else.

The lease file's *presence* is the whole signal, deliberately: it is written
before anything stops and removed after the model answers again, so there is
no window where the card is gone and nothing knows it.

The `llama-api` health check goes red for the duration of an exclusive lease —
expected, and the one thing to watch on the box: nothing may restart
`llama.service` behind the lease's back, or the leased run meets a second model
on the card. A coding or foundry lease keeps the check green; it is the same
server with other weights.

## Storage

`${DATA_DIR}/llama/models` — about 5.2 GB with the defaults, plus 12.6 GB the
first time a coding lease is taken (the Qwen weights and their drafter, kept
afterwards) and 7.7 GB the first time a foundry lease is. post-deploy
downloads to `<name>.part` and renames on completion, so an interrupted
download never leaves a truncated GGUF that llama-server would crash-loop on.

## Health checks

`/health` returns 200 only once the model and the drafter are loaded, so it
serves as both the readiness and the liveness signal. post-deploy registers
it as the `llama-api` HTTP check (60 s) on top of the auto-created
`service`-type check.
