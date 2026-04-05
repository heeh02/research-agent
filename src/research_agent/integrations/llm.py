"""Cost estimation utilities for CLI-dispatched agents.

All LLM calls go through CLI tools (claude, codex, opencode).
This module only provides pricing data and cost helpers.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens, approximate)
# ---------------------------------------------------------------------------

PRICING: dict[str, tuple[float, float]] = {
    # (input_per_1M, output_per_1M)
    # Claude
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    # OpenAI / Codex
    "gpt-5.4": (5.0, 20.0),
    "gpt-5.4-mini": (1.0, 4.0),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1": (2.0, 8.0),
    "o3": (10.0, 40.0),
    # ByteDance / Doubao (via OpenCode)
    "doubao-seed-2.0-pro": (1.5, 6.0),
    "doubao-seed-2.0-lite": (0.3, 0.6),
    "doubao-seed-2.0-code": (0.6, 2.4),
    "deepseek-v3.2": (0.5, 2.0),
    "kimi-k2.5": (1.0, 4.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = PRICING.get(model, (5.0, 15.0))
    return (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000
