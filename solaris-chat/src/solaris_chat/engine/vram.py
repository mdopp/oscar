"""The box's live GPU VRAM, for the admin panel (#367, #1332).

Since Ollama was retired there is nothing to estimate from: llama-server does
not enumerate models with disk/resident sizes the way `/api/tags` + `/api/ps`
did. So this reports what is *measured*, in order:

  1. ServiceBay's node resources (`get_system_info` -> `resources.gpus[0]`,
     queried over the admin MCP) — total and used as the node agent's
     `nvidia-smi` sees them, including KV-cache/runner overhead the chat
     container cannot see (it has no GPU of its own).
  2. `GPU_TOTAL_VRAM` env (operator override, bytes) for the total.
  3. `nvidia-smi` queried total/used (only if it somehow runs in-container).
  4. unknown -> the panel says the card did not report its memory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess


async def servicebay_gpu(url: str, token_path: str) -> tuple[int, int] | None:
    """`(total, used)` GPU VRAM in bytes from ServiceBay's node resources.

    The node agent already runs `nvidia-smi` and surfaces it under
    `resources.gpus[]` (name/memoryTotal/memoryUsed/…). We sum across GPUs.
    Fail-open: any error (MCP unreachable, no token, no GPU) returns None and
    the caller falls back to the env/nvidia-smi sources.
    """
    if not url:
        return None
    try:
        from solaris_chat.engine.tools.mcp_tools import call_sb_tool

        raw = await call_sb_tool(url, token_path, "get_system_info", {})
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — fail-open: no SB GPU => other sources
        return None
    res = data.get("resources", data) if isinstance(data, dict) else {}
    gpus = res.get("gpus") if isinstance(res, dict) else None
    if not isinstance(gpus, list) or not gpus:
        return None
    total = used = 0
    for g in gpus:
        if not isinstance(g, dict):
            continue
        t, u = g.get("memoryTotal"), g.get("memoryUsed")
        if isinstance(t, int) and t > 0:
            total += t
        if isinstance(u, int) and u >= 0:
            used += u
    return (total, used) if total else None


def _nvidia_smi_total_used() -> tuple[int, int] | None:
    """`(total, used)` GPU VRAM in bytes from `nvidia-smi`, or None."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    # Sum across GPUs; values are MiB.
    total = used = 0
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        total += int(parts[0]) * 1024 * 1024
        used += int(parts[1]) * 1024 * 1024
    return (total, used) if total else None


def gpu_total_used() -> tuple[int, int] | None:
    """`(total, used)` GPU VRAM in bytes without ServiceBay, or None.

    `GPU_TOTAL_VRAM` (env, bytes) pins the total when the operator set it;
    `used` then still has to come from `nvidia-smi`, so an env total without a
    working smi is not enough on its own.
    """
    smi = _nvidia_smi_total_used()
    env_total = os.environ.get("GPU_TOTAL_VRAM", "").strip()
    if smi is not None:
        total, used = smi
        if env_total.isdigit() and int(env_total) > 0:
            total = int(env_total)
        return total, used
    return None
