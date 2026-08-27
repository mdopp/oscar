## v8

- **Keep-alive inverted (#1264)**: `OLLAMA_KEEP_ALIVE` default lowered `24h → 15m`, and the household warm call now sends its own `keep_alive: 24h`. The service-wide setting governs **every** model any consumer on this box loads, not just ours, so a blanket 24h was a trap: one call from a neighbour that sends no per-request `keep_alive` pinned VRAM for a day, and the cost landed on the household hot path, not on the forgetful caller (box-measured 22./23.08.: gemma4:12b resident 1h50m and 2h37m for ~31 min of actual generation). Now forgetting costs minutes and whoever wants a long hold asks for it. `15m` is 3× Ollama's stock `5m` — a conversational pause still doesn't reload mid-use — and equals the model lease's `LEASE_TTL_SECONDS = 900` (#1260), so a lease that is actually being generated against never outlives its model's residency. The household fast model is unaffected: post-deploy's `warm_load_model()` and solaris-chat's lease-end re-warm both carry `FAST_MODEL_KEEP_ALIVE = "24h"` explicitly, so #268's anti-eviction guarantee is now asked for rather than imposed on the whole box.

## v7

- **Residency fix (Sol Engine Phase 0)**: `OLLAMA_MAX_LOADED_MODELS` default raised `2 → 3` and `OLLAMA_CONTEXT_LENGTH` default lowered `131072 → 32768`. Box-observed 2026-06-11: at `2`, the night crons on gemma4:12b plus embeddings left ONLY 12b loaded — the first fast (gemma4:e2b) turn of the morning paid the ~6.75s reload + cold prefill. The household stack runs three models (e2b fast chat, 12b thorough chat, nomic embed) and all must stay warm. The 131072 window existed only because the Hermes-era base prompt had grown to ~25k tokens; with the lean Sol Engine prompt (≤3k) a 32k window leaves ~29k conversation room (auto-compaction at 90%) and shrinks gemma4:12b from ≈10.3 GB to ≈8.95 GB (#214) — that KV saving is what makes the trio (≈12 GB) co-resident on the 16 GB GPU.

## v6

- **Eviction fix**: `OLLAMA_MAX_LOADED_MODELS` default raised `1 → 2`. Box-measured 2026-06-10: the v5 assumption that the embed model runs in a separate runner "not counted against this chat-model slot" was **wrong** — the cap is GLOBAL. At `1`, any embedding (notes/memory/search uses `nomic-embed-text`) evicts `gemma4:e2b`, and the next chat turn pays a **~6.75s model reload + ~2.6s cold prefill (~9.4s turns)**. `2` keeps gemma4:e2b (~2.15 GB) + nomic (~0.3 GB) co-resident — trivial on the 16 GB GPU. The v5 chat↔chat concern only applies if a second *chat* model (gemma4:12b) must also stay warm → then use `3`.

## v5

- **Anti-eviction (#268)**: `OLLAMA_KEEP_ALIVE` default raised from `60m` to `24h` — an overnight gap no longer evicts the chat model, so the cached prefix and loaded weights survive across the morning's first turn (the 60m cap existed only to let a co-resident idle model release VRAM; with the new `OLLAMA_MAX_LOADED_MODELS=1` there is no second chat model to co-reside, so the cap is unnecessary).
- **Anti-eviction (#268)**: new `OLLAMA_MAX_LOADED_MODELS` variable (default `1`). Keeps exactly one chat model resident so switching gemma4:12b ↔ gemma4:e2b can never co-reside and evict the cached-prefix model under the tight 128k VRAM footprint — the chat↔chat eviction race that defeated KV-prefix reuse across turns. The dedicated embed model (`OLLAMA_EMBED_MODEL`) runs in its own llama-server runner and is not counted against this chat-model slot, so embeddings still serve in parallel. Wired on both the `.kube` and GPU `.container` render paths.

## v4

- **Latency bundle**: `OLLAMA_CONTEXT_LENGTH` default raised from `32768` back to `131072` (128k). The base system prompt grew to ~25k tokens, so at 32k only ~7k was left for the conversation — the prompt brushed/crossed the window every HA-control turn, forcing llama.cpp drop-middle truncation → KV-cache invalidation → a full re-prefill each turn (~20s) and shearing the tool block ('no tool call'). 128k gives ~103k conversation room so the cached prefix survives. Box-measured (#214): gemma4:12b @131072 ≈10.3 GB on a 16 GB GPU. VRAM caveat: gemma4:12b + gemma4:e2b co-resident @128k ≈14.6 GB of ~15.6 GB usable — tight; `OLLAMA_KEEP_ALIVE` governs pinning, and if it OOMs the fallback is keeping e2b resident with 12b at a smaller window / on demand.
- **Latency bundle**: new `OLLAMA_KEEP_ALIVE` variable (default `60m`). Stock `5m` evicts the chat model after a short pause, so the next turn pays a cold model RELOAD on top of prefill. 60m survives any realistic conversational pause; deliberately NOT `-1` so a genuinely idle co-resident model can still release VRAM under the tight 128k footprint. Wired on both the `.kube` and GPU `.container` render paths.

### Earlier v4 (now superseded by the latency bundle above)

- `OLLAMA_CONTEXT_LENGTH` default lowered from `131072` to `32768` (#214). gemma4:12b's true native context is `262144` (not 131072); 32768 covers Hermes' ~4157-token system prompt plus a long conversation, loads gemma4:12b 100% on a 16 GB GPU at ~8.95 GB (vs ~10.3 GB at 131072), leaves ~6.6 GB headroom for the embedding model, and avoids the eviction race at an oversized window that was bouncing the box to `gemma4:e2b`.
- New `OLLAMA_FLASH_ATTENTION` variable (default `1`) — enables flash attention (prerequisite for optional KV-cache quant; negligible speed change on this GPU). Wired on both the `.kube` and GPU `.container` render paths.
- New `OLLAMA_EMBED_MODEL` variable (default `nomic-embed-text`), pre-pulled at install. Embeddings/RAG must target this tag, never the chat model: a distinct model gets its own llama-server runner that serves in parallel with a chat generation, so embeds don't serialize behind it (#214). Do NOT raise `OLLAMA_NUM_PARALLEL` or run a second Ollama.

## v3

- `OLLAMA_DEFAULT_MODEL` default bumped to `gemma4:12b` (newer 12B parameter natively multimodal model, with `gemma4:e2b` supported as a lighter alternative).
- `OLLAMA_EXTRA_MODELS` default changed to empty string (`""`) so only one model is loaded by default.

## v2

- New `OLLAMA_EXTRA_MODELS` variable (CSV) — additional models pre-pulled at install time on top of `OLLAMA_DEFAULT_MODEL`. Default ships a quantized 26B model that fits 100% on a 16 GB GPU, giving the operator a one-click "smarter but slower" choice in Hermes' Models tab without a fresh download (#1046). Note: the 26B-VRAM variant is text-only — multimodal stays on `gemma4:e4b`.
- `OLLAMA_DEFAULT_MODEL` default bumped from `gemma3:4b` to `gemma4:e4b` — same VRAM class, newer architecture, AND native multimodal (`ollama show gemma4:e4b` reports `completion, vision, audio, tools, thinking`). The default bump alone gets a fresh install most of the way through #923 / #938: vision-capable LLM on disk without needing a separate `OLLAMA_VISION_MODEL` pull.
- `OLLAMA_VISION_MODEL` demoted to "only set if you've changed the default to a text-only tag"; description updated accordingly.
- `pull_model` now verifies the tag is present in `/api/tags` *after* the streaming pull reports success. The CLI and the HTTP streaming endpoint both occasionally report success while manifest write fails silently (e.g. `library/<namespace>/` left root-owned by an earlier rootful run); the unverified happy path left operators with a `HTTP 404: model not found` on first chat. Now we fail loud (#1047).

## v1

- Initial template — single model pull (`OLLAMA_DEFAULT_MODEL`), optional vision model (`OLLAMA_VISION_MODEL`), GPU passthrough toggle.
