# `solaris-whisper-batch` — the batch transcription endpoint

The contract for the **second** whisper container: a `large-v3-turbo` model that
turns an hours-long recording into timestamped segments, on the GPU **only while
there is work**. It is not the household voice path — that is the Wyoming server
on `:10300` and it is untouched by anything here.

> **Source of truth is the code, not this file:** `WHISPER_BATCH_MODULE` in
> `templates/solaris/post-deploy.py`. Fields are added, never renamed or removed.

## Availability

- **Off by default.** The operator turns it on with the `WHISPER_BATCH_ENABLED`
  template variable; the choice is remembered on the box in
  `<data>/stacks/solarisbay/.whisper-batch-enabled`, because ServiceBay keeps no
  per-template variables. It also needs a CDI GPU and an existing recordings
  directory — without either the unit is removed rather than left running.
- **`http://127.0.0.1:10301/transcribe`, loopback only.** The container is on
  the host network; the endpoint reads the household's session recordings, so it
  is not on the LAN. Its caller runs on the same box.

## `POST /transcribe`

```json
{
  "path": "sitzung4-20260818T201032-Daniel.wav",
  "language": "de",
  "hotwords": ["Lorok", "Zinnblick", "…"],
  "vad": true
}
```

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | The recording, relative to (or inside) the mounted recordings root. Resolved with `realpath` and refused with **403** if it leaves the mount — `../`, an absolute path outside it, and a symlink pointing out are all refused, not followed. |
| `language` | no | e.g. `"de"`. Omitted ⇒ whisper detects it. |
| `hotwords` | no | The round's name register. Over-budget names are **dropped whole and reported** (see `hotwords_dropped`), never cut mid-name. |
| `vad` | no, **defaults to `true`** | Silence detection: drop non-speech before the decoder sees it. |

### Why `vad` defaults to on

Hotwords prime the decoder to emit the register when it hears nothing, and on a
per-speaker track most of a session *is* nothing — the other four talking. The
caller cannot tell an invented name from a spoken one; only this side sees the
audio. So the cut happens here, on by default, and the amount is reported back.

Parameters (faster-whisper 1.2.1 defaults, pinned in the module because the
image auto-updates): `threshold 0.5`, `min_silence_duration_ms 2000`,
`speech_pad_ms 400`. None of them is tightened — the opposite failure, losing a
quiet sentence, is the worse one. `"vad": false` restores the raw decoder for a
caller that wants to compare.

### Response — 200

```json
{
  "segments": [{"start": 0.0, "end": 13.7, "text": "…"}],
  "hotwords_dropped_count": 0,
  "hotwords_dropped": [],
  "vad": true,
  "audio_seconds": 173.1,
  "silence_dropped_seconds": 148.9
}
```

| Field | Meaning |
|---|---|
| `segments` | `start`/`end` in **seconds, relative to the submitted file** — the service is stateless and never invents a session offset. The caller splits a session into chunks and adds each chunk's own offset. |
| `hotwords_dropped_count` / `hotwords_dropped` | The names that did not fit faster-whisper's hotword budget. |
| `vad` | What was actually applied for this request. |
| `audio_seconds` | Length of the decoded audio. |
| `silence_dropped_seconds` | How much of it the silence detection discarded — `0` when `vad` is `false`. |

### Errors

| Code | When |
|---|---|
| 400 | Body is not JSON. |
| 403 | `path` is not a regular file inside the recordings mount. |
| 404 | Not `/transcribe`. |
| 500 | This one file failed; the service stays up. |
| 503 | The GPU worker could not be started or died before it answered. |

## Model lifecycle — the card is borrowed, not held

The endpoint process holds no model. The **first** request starts a worker child
that loads `large-v3-turbo` onto the card; the worker is stopped once no request
has arrived for `WHISPER_BATCH_IDLE_S` (default **300 s**), and stopping it is
what returns the 2216 MiB to the driver — CTranslate2's CUDA allocator caches
every block it frees, so unloading the model inside a long-lived process would
give the card back nothing.

What this costs the caller: **one model load per idle period**, not per request.
A session submitted as back-to-back chunks pays it once, on the first chunk; the
rest run against the warm worker. The recording itself decodes at 23.7x realtime
(box-measured), so a 4h track is ~10 min of work either way.

Requests are **serialised** — one model, one card. A second request waits for the
first, it is not refused.

## What the service keeps

Nothing. The recording is read in place through a read-only mount, the segments
live in memory until the response is written — no temp copy, no transcript on
disk — and the access log line carries the method and the endpoint but never the
path (a speaker's name) nor the text.
