"""The embeddings server the `llama` template grew when Ollama was retired
(#1332).

The load-bearing part is vector compatibility, not the unit plumbing: the
~46k rows already in `okf_vectors` were produced by Ollama's
`nomic-embed-text` tag — v1.5, f16, 768 dims, mean pooling, raw text with no
`search_document:`/`search_query:` prefix. Same model, same quant, same
pooling ⇒ nothing had to be re-embedded. A smaller quant or a different
pooling would not fail; it would quietly make search worse, which is why both
are pinned here.

`--ubatch-size` == the context length is the other non-negotiable: an
embedding model runs non-causal attention and llama.cpp refuses a request
longer than one micro-batch.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("llama_pd_embed", TEMPLATES / "llama" / "post-deploy.py")


def test_the_default_embedding_model_is_the_one_ollama_served(pd):
    profile = pd.embed_profile()
    assert profile["model_file"] == "nomic-embed-text-v1.5.f16.gguf"
    assert profile["alias"] == "nomic-embed-text"
    assert profile["context_length"] == "2048"


def test_server_args_pin_mean_pooling_and_a_full_size_micro_batch(pd):
    args = pd.embed_server_args("/models")
    assert "--embeddings" in args
    assert args[args.index("--pooling") + 1] == "mean"
    ctx = args[args.index("-c") + 1]
    assert args[args.index("--ubatch-size") + 1] == ctx
    assert args[args.index("--batch-size") + 1] == ctx


def test_the_embeddings_server_binds_loopback_only(pd):
    """Its one consumer is the Solaris Engine, which shares this host's netns.
    The chat server's LAN-facing bind (#1344) exists for isolated pods; nothing
    reaches this one, so there is nothing to open."""
    args = pd.embed_server_args("/models")
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--port") + 1] == "11436"


def test_the_unit_carries_both_gpu_lines_or_neither(pd):
    gpu = pd.render_embed_container_unit("/mnt/data/stacks", gpu=True)
    assert "AddDevice=nvidia.com/gpu=all" in gpu
    assert "SecurityLabelDisable=true" in gpu
    assert "ContainerName=llama-embed" in gpu
    assert "Volume=/mnt/data/stacks/llama/models:/models:Z" in gpu

    cpu = pd.render_embed_container_unit("/mnt/data/stacks", gpu=False)
    assert "AddDevice" not in cpu
    assert "SecurityLabelDisable" not in cpu


def test_install_is_idempotent_and_starts_the_unit(pd, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        pd.subprocess, "run", lambda argv, **kw: calls.append(list(argv)) or _Done()
    )
    unit = tmp_path / ".config" / "containers" / "systemd" / "llama-embed.container"

    assert pd.install_embed_unit("/mnt/data/stacks", gpu=True) is True
    assert unit.exists()
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "restart", "llama-embed.service"] in calls

    calls.clear()
    assert pd.install_embed_unit("/mnt/data/stacks", gpu=True) is True
    # Unchanged file => no rewrite, no reload, and a plain start rather than a
    # restart: a redeploy must not drop the embeddings mid-ingest.
    assert ["systemctl", "--user", "daemon-reload"] not in calls
    assert calls == [["systemctl", "--user", "start", "llama-embed.service"]]


def test_an_empty_port_skips_the_embeddings_server(pd, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLAMA_EMBED_PORT", "")
    assert pd.install_embed_unit("/mnt/data/stacks", gpu=True) is False
    assert not (tmp_path / ".config" / "containers" / "systemd").exists()


def test_readiness_probes_the_embeddings_endpoint_not_health(pd, monkeypatch):
    """`/health` says the model loaded; it does not say the server was started
    with `--embeddings`. Without the flag every embed comes back 501 while the
    unit looks perfectly healthy."""
    seen: list[str] = []

    def fake(url, payload=None, method="GET", timeout=10.0, extra_headers=None):
        seen.append(url)
        return 200, b"{}"

    monkeypatch.setattr(pd, "http_request", fake)
    assert pd.embed_reachable("11436") is True
    assert seen == ["http://127.0.0.1:11436/v1/embeddings"]
