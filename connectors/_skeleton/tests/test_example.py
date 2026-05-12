"""Skeleton test. Copy and rename per concrete tool.

Conventions:
- Use httpx-mock for any external API call — no real network in CI.
- Test the happy path, the external-error path, and the auth path
  (bearer-less request → 401).
"""

import pytest


@pytest.mark.asyncio
async def test_example_happy_path():
    # from CONNECTOR_NAME.tools.example import run, ExampleInput
    # result = await run(ExampleInput(query="hello"), ctx=...)
    # assert result.result == "..."
    pass
