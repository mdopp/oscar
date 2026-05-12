"""Per-million-token prices (USD) for cost estimation in cloud_audit.

Pricing tables drift. These are good enough for "is this getting expensive?"
ballpark logging, not for billing. Re-check upstream pages quarterly.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenPrice:
    input_per_mtok_usd: float
    output_per_mtok_usd: float


# Last refreshed: 2026-05.
ANTHROPIC: dict[str, TokenPrice] = {
    "claude-sonnet-4": TokenPrice(3.0, 15.0),
    "claude-haiku-4-5": TokenPrice(0.80, 4.0),
    "claude-opus-4-7": TokenPrice(15.0, 75.0),
}

GOOGLE: dict[str, TokenPrice] = {
    "gemini-2.5-flash": TokenPrice(0.075, 0.30),
    "gemini-2.5-pro": TokenPrice(1.25, 5.0),
}


def cost_micro_usd(
    vendor: str, model: str, input_tokens: int, output_tokens: int
) -> int | None:
    """Return cost in micro-USD (millionths of a dollar), or None if model not in the table."""
    table = {"anthropic": ANTHROPIC, "google": GOOGLE}.get(vendor)
    if not table:
        return None
    price = table.get(model)
    if not price:
        return None
    usd = (
        input_tokens * price.input_per_mtok_usd
        + output_tokens * price.output_per_mtok_usd
    ) / 1_000_000
    return round(usd * 1_000_000)
