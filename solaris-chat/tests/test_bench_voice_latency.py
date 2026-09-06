"""The pure halves of the speech-end-to-answer-start bench (#1128).

The measurement itself needs the box; what can be pinned here is the audio
conversion between Kokoro (24 kHz) and Wyoming STT (16 kHz), the percentile
convention #1120's tables were published with, and the ten commands the run
reports on.
"""

from __future__ import annotations

import importlib.util
import io
import wave
from array import array
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_voice_latency.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_voice_latency", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wav(samples: list[int], rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array("h", samples).tobytes())
    return out.getvalue()


def test_wav_to_pcm_reads_the_kokoro_container(bench):
    data = _wav([0, 1000, -1000, 32767], 24000)
    pcm, rate = bench.wav_to_pcm(data)
    assert rate == 24000
    assert list(array("h", pcm)) == [0, 1000, -1000, 32767]


def test_wav_to_pcm_rejects_stereo(bench):
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00\x00\x00")
    with pytest.raises(ValueError):
        bench.wav_to_pcm(out.getvalue())


def test_resample_24k_to_16k_keeps_two_thirds_of_the_samples(bench):
    src = array("h", [i * 10 for i in range(300)])
    out = array("h", bench.resample(src.tobytes(), 24000))
    assert len(out) == 200
    # a linear ramp stays a ramp: sample i lands on source position i * 1.5
    for i in (0, 1, 50, 199):
        assert out[i] == pytest.approx(i * 15, abs=1)


def test_resample_is_a_no_op_at_the_target_rate(bench):
    pcm = array("h", [1, 2, 3]).tobytes()
    assert bench.resample(pcm, 16000) is pcm


def test_resample_survives_an_empty_utterance(bench):
    assert bench.resample(b"", 24000) == b""


def test_percentiles_match_the_bench_models_convention(bench):
    """The #1120 tables were published with `sorted(v)[int(0.95 * n) - 1]`;
    a different rule here would make the two sets unreadable against each other."""
    values = [float(i) for i in range(1, 11)]
    assert bench.p50(values) == 5.5
    assert bench.p95(values) == 9.0
    assert bench.p95([2.0]) == 2.0
    assert bench.p95([3.0, 1.0, 2.0]) == 2.0  # order does not matter
    for n in (10, 15, 20):
        sample = [float(i) for i in range(n)]
        assert bench.p95(sample) == sample[int(n * 0.95) - 1]


def test_read_event_takes_the_data_the_server_announces(bench):
    """wyoming answers with `data_length` bytes after the header; reading only
    the inline `data` returns an empty transcript with plausible timing."""
    body = b'{"text": "Schalte das Buerolicht ein."}'
    wire = (
        (b'{"type": "transcript", "data_length": %d}\n' % len(body))
        + body
        + b'{"type": "ignored"}\n'
    )
    etype, data = bench._read_event(io.BytesIO(wire))
    assert (etype, data["text"]) == ("transcript", "Schalte das Buerolicht ein.")


def test_read_event_also_takes_inline_data_and_skips_payloads(bench):
    wire = b'{"type": "audio-chunk", "data": {"rate": 16000}, "payload_length": 3}\nabc'
    stream = io.BytesIO(wire)
    etype, data = bench._read_event(stream)
    assert (etype, data) == ("audio-chunk", {"rate": 16000})
    assert stream.read() == b""


def test_read_event_refuses_a_closed_connection(bench):
    with pytest.raises(RuntimeError, match="closed the connection"):
        bench._read_event(io.BytesIO(b""))


def test_ten_commands_and_ten_runs(bench):
    assert len(bench.COMMANDS) == 10
    assert bench.RUNS == 10
    assert bench.TARGET_RATE == 16000
    # no heating sentence: this household has no climate device (#1128)
    assert not any("Heizung" in c for c in bench.COMMANDS)


def test_table_marks_only_the_rows_over_the_threshold(bench):
    rows = [
        {
            "text": t,
            "stt_p50": 0.3,
            "stt_p95": 0.4,
            "ttft_p50": ttft,
            "ttft_p95": ttft,
            "total_p50": 0.3 + ttft,
            "total_p95": 0.4 + ttft,
            "tool": "–",
        }
        for t, ttft in (("schnell", 0.5), ("knapp", 0.95), ("langsam", 2.0))
    ]
    table = bench.markdown_table(rows).splitlines()
    assert table[2].endswith("| – | nein |")
    assert table[3].endswith("| – | knapp (p95) |")
    assert table[4].endswith("| – | **ja** |")
