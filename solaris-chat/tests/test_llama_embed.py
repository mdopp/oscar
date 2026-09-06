"""The embeddings client (#1332) — the OpenAI `/v1/embeddings` shape, and the
one thing that had to stay identical to the Ollama era: the text.

Every vector in `okf_vectors` was computed by Ollama's `nomic-embed-text` on
the RAW query/document text. nomic's model card describes `search_document:` /
`search_query:` prefixes; Ollama never applied them, so adding one here would
move new vectors away from the stored ones — search would not fail, it would
quietly get worse. The prefix test is the guard on that.
"""

from __future__ import annotations

import pytest

from solaris_chat.engine import llama_embed
from solaris_chat.engine.llama_embed import LlamaEmbed
from solaris_chat.engine.llama_server import LlamaServerError


def _patch_post(monkeypatch, body, status=200):
    class _Resp:
        def __init__(self):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return body

        async def text(self):
            return "boom"

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None):
            _Session.last = {"url": url, "json": json}
            return _Resp()

    monkeypatch.setattr(llama_embed.aiohttp, "ClientSession", _Session)
    return _Session


async def test_embeds_a_batch_and_sends_the_text_unprefixed(monkeypatch):
    sess = _patch_post(
        monkeypatch,
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        },
    )
    out = await LlamaEmbed("http://x:11436").embed("nomic-embed-text", ["a", "b"])

    assert out == [[0.1, 0.2], [0.3, 0.4]]
    assert sess.last["url"] == "http://x:11436/v1/embeddings"
    assert sess.last["json"] == {"model": "nomic-embed-text", "input": ["a", "b"]}


async def test_vectors_come_back_in_request_order(monkeypatch):
    """The caller zips these against its own batch, so an out-of-order response
    would write every vector onto the wrong concept."""
    _patch_post(
        monkeypatch,
        {
            "data": [
                {"index": 1, "embedding": [2.0]},
                {"index": 0, "embedding": [1.0]},
            ]
        },
    )
    out = await LlamaEmbed("http://x:11436").embed("nomic-embed-text", ["a", "b"])
    assert out == [[1.0], [2.0]]


async def test_a_non_2xx_raises_so_the_queue_survives(monkeypatch):
    _patch_post(monkeypatch, {}, status=503)
    with pytest.raises(LlamaServerError):
        await LlamaEmbed("http://x:11436").embed("nomic-embed-text", ["a"])
