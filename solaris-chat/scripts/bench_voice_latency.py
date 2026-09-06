#!/usr/bin/env python3
"""Speech end -> answer start, for the ten commands the house actually says (#1128).

A5's Definition of Done is one number the earlier runs never produced: the wait
a resident feels, from the moment they stop talking to the moment Solaris starts
answering. #1120's model-half tables measured only the engine, and the operator's
spoken round in #1128 could not be read back — Home Assistant keeps pipeline
timings in memory for the last ten runs and had none left. This bench replaces
the microphone with a file and measures both halves end to end:

    solaris-tts (Kokoro Martin, :8881)  -> a WAV on disk, never played
      -> solaris-whisper (Wyoming, :10300, the STT entity HA's pipeline dials)
        -> llama-server (:11435, gemma-4-e4b + MTP, the household chat backend)

    t_stt   audio-stop on the wire -> `transcript` event back
    t_ttft  transcript in          -> first content or tool-call delta out
    total   t_stt + t_ttft         <- the DoD number

Streaming the audio faster than real time does not flatter t_stt:
wyoming-faster-whisper buffers the utterance and does all of its work after
`audio-stop`, which is exactly the residue a live speaker waits through.

**Nothing is executed.** The engine half calls `LlamaServerChat` directly with
the production toolbox and reads `tool_calls` only — the same non-executing path
`bench_tool_decision.py` uses. No light switches, no cover moves, no media
starts, no notification is sent, and the rendered speech is never played.

The prompt is `bench_models.py`'s: `build_engine_clients()`'s household toolbox,
the shipped/operator SOUL.md and the real registry render, so the prefill this
reports is the one the engine pays. `check_shape()` refuses the run if that
assembly has drifted.

Validity: a foundry or coding GPU lease (#1320) moves the card away from the
household model, so the run refuses to start while one is held.

It runs inside the `solaris-chat` container — that is where `solaris_chat` is
importable and where 127.0.0.1 reaches all three services. Copy it in through
the ServiceBay data mount (`/mnt/data/stacks/solarisbay` is the container's
`/var/lib/solaris`), then one command:

    write_file /mnt/data/stacks/solarisbay/bench_voice_latency.py   <- this file
    write_file /mnt/data/stacks/solarisbay/bench_models.py          <- it imports that
    container_exec solaris-chat: python3 /var/lib/solaris/bench_voice_latency.py

Delete both copies afterwards; the WAVs go to a temp dir inside the container
and are removed on the way out unless `--keep-wavs`. Takes about ten minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import shutil
import socket
import statistics
import sys
import time
import urllib.request
import wave
from array import array
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

# The ten the operator was asked to speak in #1128 — one no-tool question, the
# four light commands the house really has (Bürolicht, Sofalicht, Küchenlicht,
# Terrassenlicht are all in whisper's own hint list on the box), a timer, music,
# the shopping list, and the two calendar turns. The heating sentence from the
# model-half table is deliberately absent: there is no climate device in this
# household, so it would measure a house that does not exist.
COMMANDS = [
    "Wie heißt die Hauptstadt von Australien?",
    "Schalte das Bürolicht ein.",
    "Mach das Sofalicht aus.",
    "Ist das Küchenlicht an?",
    "Mach das Terrassenlicht an.",
    "Stell einen Timer auf zehn Minuten.",
    "Spiel Musik in der Küche.",
    "Setz Milch auf die Einkaufsliste.",
    "Was steht morgen an?",
    "Trag am Freitag um achtzehn Uhr Zahnarzt ein.",
]
RUNS = 10
TARGET_RATE = 16000  # what Wyoming STT is fed; Kokoro renders at 24 kHz
SLOW_S = 1.3  # the threshold #1128 asks to be marked, not a pass/fail


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """Mono 16-bit frames + sample rate out of a WAV container."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit PCM from the TTS")
        return handle.readframes(handle.getnframes()), handle.getframerate()


def resample(pcm: bytes, rate: int, target: int = TARGET_RATE) -> bytes:
    """Linear-interpolation resample of mono 16-bit PCM.

    Kokoro speaks at 24 kHz and Wyoming STT wants 16 kHz. Interpolating rather
    than dropping every third sample keeps the aliasing out of the transcript,
    and it is a handful of stdlib lines against a dependency the runtime image
    does not carry.
    """
    if rate == target:
        return pcm
    src = array("h")
    src.frombytes(pcm)
    if not src:
        return pcm
    count = len(src) * target // rate
    out = array("h", bytes(2 * count))
    step = rate / target
    last = len(src) - 1
    for i in range(count):
        pos = i * step
        left = int(pos)
        right = min(left + 1, last)
        frac = pos - left
        out[i] = int(src[left] + (src[right] - src[left]) * frac)
    return out.tobytes()


def p50(values: list[float]) -> float:
    return statistics.median(values)


def p95(values: list[float]) -> float:
    """`sorted(v)[int(0.95 * n) - 1]` — bench_models.py's rule, kept identical
    so this table can be read against the ones already posted on #1120."""
    ordered = sorted(values)
    return ordered[int(len(ordered) * 0.95) - 1]


def markdown_table(rows: list[dict]) -> str:
    """The results table as it is posted on #1128 / #1120."""
    head = (
        "| Befehl | t_stt p50/p95 (s) | t_ttft p50/p95 (s) | "
        "**total p50/p95 (s)** | Werkzeug | >1,3 s? |\n"
        "|---|---:|---:|---:|---|---|"
    )
    lines = [head]
    for row in rows:
        flag = "**ja**" if row["total_p50"] > SLOW_S else "nein"
        if row["total_p50"] <= SLOW_S < row["total_p95"]:
            flag = "knapp (p95)"
        label = "„" + row["text"] + "“"
        lines.append(
            f"| {label}"
            f" | {row['stt_p50']:.2f} / {row['stt_p95']:.2f}"
            f" | {row['ttft_p50']:.2f} / {row['ttft_p95']:.2f}"
            f" | **{row['total_p50']:.2f} / {row['total_p95']:.2f}**"
            f" | {row['tool']} | {flag} |"
        )
    return "\n".join(lines)


def _send(sock: socket.socket, etype: str, data=None, payload: bytes | None = None):
    header: dict = {"type": etype}
    if data is not None:
        header["data"] = data
    if payload is not None:
        header["payload_length"] = len(payload)
    sock.sendall((json.dumps(header) + "\n").encode("utf-8"))
    if payload is not None:
        sock.sendall(payload)


def _read_event(stream) -> tuple[str, dict]:
    """One Wyoming event off the wire.

    The header may carry its `data` inline (what a client may send) or announce
    it as `data_length` bytes that follow the newline — which is how the server
    answers. Reading only the inline form yields an empty transcript with the
    right timing, so a run looks healthy and says nothing.
    """
    line = stream.readline()
    if not line:
        raise RuntimeError("whisper closed the connection before a transcript")
    header = json.loads(line.decode("utf-8"))
    data = header.get("data") or {}
    if header.get("data_length"):
        data = json.loads(stream.read(header["data_length"]).decode("utf-8"))
    if header.get("payload_length"):
        stream.read(header["payload_length"])
    return str(header.get("type") or ""), data


def transcribe(host: str, port: int, pcm: bytes, language: str) -> tuple[str, float]:
    """One Wyoming transcription; returns the text and the seconds after
    `audio-stop` — the part of STT a live speaker actually waits through."""
    sock = socket.create_connection((host, port), timeout=30)
    sock.settimeout(60)
    try:
        audio = {"rate": TARGET_RATE, "width": 2, "channels": 1, "timestamp": 0}
        _send(sock, "transcribe", {"language": language})
        _send(sock, "audio-start", audio)
        for off in range(0, len(pcm), 4000):
            _send(
                sock, "audio-chunk", dict(audio, timestamp=off), pcm[off : off + 4000]
            )
        _send(sock, "audio-stop", {"timestamp": len(pcm)})
        started = time.monotonic()
        stream = sock.makefile("rb")
        while True:
            etype, data = _read_event(stream)
            if etype == "transcript":
                text = str(data.get("text") or "")
                return text.strip(), time.monotonic() - started
    finally:
        sock.close()


def synthesize(tts_url: str, text: str) -> bytes:
    request = urllib.request.Request(
        f"{tts_url.rstrip('/')}/v1/audio/speech",
        data=json.dumps({"input": text, "response_format": "wav"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _bench_models():
    spec = importlib.util.spec_from_file_location(
        "bench_models", _SCRIPT_DIR / "bench_models.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _answer(chat, model, system, tools, text, temperature):
    """One turn, generation only — `tool_calls` is read, never dispatched."""
    options = {"temperature": temperature} if temperature is not None else None
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    result = None
    async for kind, payload in chat.stream(
        model, messages, tools=tools, think=False, options=options
    ):
        if kind == "done":
            result = payload
    return result


def _refuse_if_leased() -> None:
    from solaris_chat import gpu_lease
    from solaris_chat.config import settings

    path = gpu_lease.lease_path(settings.solaris_db_path)
    holder = gpu_lease.holder(path)
    if holder:
        raise SystemExit(
            f"GPU lease held by {holder} — the household model is not on the card, "
            "so these latencies would describe something else. Re-run when it is free."
        )


def _prepare(args) -> tuple:
    """Prompt assembly, outside any event loop: `bench_models.build_system()`
    drives its own `asyncio.run`, which raises if one is already running."""
    _refuse_if_leased()
    bench = _bench_models()
    household = bench.build_household(args.ollama_url)
    system = bench.build_system(household)
    tools = household._profile.toolbox.definitions()
    est = bench.check_shape(household, system)
    model = args.model or household._profile.model
    return system, tools, est, model, household._profile.temperature


async def _run(args, system, tools, est, model, temperature) -> int:
    from solaris_chat.engine.llama_server import LlamaServerChat

    chat = LlamaServerChat(args.llama_url)

    wav_dir = Path(args.wav_dir)
    wav_dir.mkdir(parents=True, exist_ok=True)
    print(f"model {model}, {len(tools)} tools, ~{est} est-tok prompt")
    print(f"rendering {len(COMMANDS)} commands to {wav_dir} (never played)")

    audio: list[bytes] = []
    for index, text in enumerate(COMMANDS):
        data = synthesize(args.tts_url, text)
        (wav_dir / f"cmd{index:02d}.wav").write_bytes(data)
        pcm, rate = wav_to_pcm(data)
        audio.append(resample(pcm, rate))

    # One throwaway pass so neither whisper nor the prefix cache is cold.
    transcribe(args.stt_host, args.stt_port, audio[0], args.language)
    await _answer(chat, model, system, tools, COMMANDS[0], temperature)

    rows: list[dict] = []
    prefills: list[int] = []
    totals: list[float] = []
    transcripts: list[str] = []
    for text, pcm in zip(COMMANDS, audio):
        stt: list[float] = []
        ttft: list[float] = []
        row_totals: list[float] = []
        tools_seen: set[str] = set()
        heard = ""
        for _ in range(args.runs):
            heard, t_stt = transcribe(args.stt_host, args.stt_port, pcm, args.language)
            result = await _answer(chat, model, system, tools, heard, temperature)
            stt.append(t_stt)
            ttft.append(result.ttft_s)
            row_totals.append(t_stt + result.ttft_s)
            if result.prompt_tokens:
                prefills.append(result.prompt_tokens)
            tools_seen.update(c["function"]["name"] for c in result.tool_calls)
        totals += row_totals
        transcripts.append(heard)
        rows.append(
            {
                "text": text,
                "stt_p50": p50(stt),
                "stt_p95": p95(stt),
                "ttft_p50": p50(ttft),
                "ttft_p95": p95(ttft),
                "total_p50": p50(row_totals),
                "total_p95": p95(row_totals),
                "tool": ", ".join(sorted(tools_seen)) or "–",
            }
        )
        print(f"  done {text!r} -> {heard!r}")

    print()
    print(markdown_table(rows))
    print()
    print(f"n={len(totals)} · overall total p50 {p50(totals):.2f} s · ", end="")
    print(f"p95 {p95(totals):.2f} s")
    print(f"Prefill (llama-server prompt_tokens, turn 1): {min(prefills)} tok")
    for text, heard in zip(COMMANDS, transcripts):
        if heard.rstrip(".?!").lower() != text.rstrip(".?!").lower():
            print(f"  STT drift: {text!r} -> {heard!r}")
    if not args.keep_wavs:
        shutil.rmtree(wav_dir, ignore_errors=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tts-url", default="http://127.0.0.1:8881")
    ap.add_argument("--stt-host", default="127.0.0.1")
    ap.add_argument("--stt-port", type=int, default=10300)
    ap.add_argument("--llama-url", default="http://127.0.0.1:11435")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="", help="default: the household profile's")
    ap.add_argument("--language", default="de")
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--wav-dir", default="/tmp/solaris-voice-bench")
    ap.add_argument("--keep-wavs", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args, *_prepare(args))))


if __name__ == "__main__":
    main()
