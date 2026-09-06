"""What the admin panel's VRAM readout is allowed to claim (#367, #1332).

Since Ollama went, there is no model inventory to estimate a footprint from —
`/api/tags` + `/api/ps` had disk and resident sizes per tag, llama-server has
nothing equivalent. So the readout reports only what was *measured*, and these
tests pin that: ServiceBay's node agent first, `nvidia-smi` second, and "the
card did not report its memory" rather than a number nobody measured.
"""

from __future__ import annotations

import json

from solaris_chat.engine import vram

GIB = 1024 * 1024 * 1024


async def test_servicebay_gpu_sums_resources(monkeypatch):
    import solaris_chat.engine.tools.mcp_tools as mcp

    async def fake_call(url, token_path, name, arguments):
        assert name == "get_system_info"
        return json.dumps(
            {"resources": {"gpus": [{"memoryTotal": 16 * GIB, "memoryUsed": 10 * GIB}]}}
        )

    monkeypatch.setattr(mcp, "call_sb_tool", fake_call)
    assert await vram.servicebay_gpu("http://sb", "/tok") == (16 * GIB, 10 * GIB)


async def test_servicebay_gpu_none_when_no_url_or_no_gpu(monkeypatch):
    assert await vram.servicebay_gpu("", "/tok") is None

    import solaris_chat.engine.tools.mcp_tools as mcp

    async def no_gpu(url, token_path, name, arguments):
        return json.dumps({"resources": {"gpus": []}})

    monkeypatch.setattr(mcp, "call_sb_tool", no_gpu)
    assert await vram.servicebay_gpu("http://sb", "/tok") is None


def test_local_fallback_reads_nvidia_smi(monkeypatch):
    monkeypatch.delenv("GPU_TOTAL_VRAM", raising=False)
    monkeypatch.setattr(vram.shutil, "which", lambda _: "/usr/bin/nvidia-smi")

    class _Done:
        returncode = 0
        stdout = "16380, 10240\n"

    monkeypatch.setattr(vram.subprocess, "run", lambda *a, **k: _Done())
    total, used = vram.gpu_total_used()
    assert (total, used) == (16380 * 1024 * 1024, 10240 * 1024 * 1024)


def test_an_env_total_overrides_only_the_total(monkeypatch):
    """`GPU_TOTAL_VRAM` pins what the card has; how much is in USE still has to
    be measured, so an env total on its own is not a source."""
    monkeypatch.setenv("GPU_TOTAL_VRAM", str(24 * GIB))
    monkeypatch.setattr(vram.shutil, "which", lambda _: "/usr/bin/nvidia-smi")

    class _Done:
        returncode = 0
        stdout = "16380, 10240\n"

    monkeypatch.setattr(vram.subprocess, "run", lambda *a, **k: _Done())
    assert vram.gpu_total_used() == (24 * GIB, 10240 * 1024 * 1024)


def test_unknown_without_smi(monkeypatch):
    monkeypatch.setenv("GPU_TOTAL_VRAM", str(16 * GIB))
    monkeypatch.setattr(vram.shutil, "which", lambda _: None)
    assert vram.gpu_total_used() is None
