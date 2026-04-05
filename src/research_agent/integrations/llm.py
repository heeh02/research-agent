"""LLM provider abstraction — Claude, OpenAI, ByteDance Seedance, and extensible.

Key design: different agents can use different providers/models.
ByteDance Seedance 2.0 Pro uses an OpenAI-compatible API (Volcano Engine ARK).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..models import CostRecord, LLMProvider, AgentRole, Stage


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens, approximate)
# ---------------------------------------------------------------------------

PRICING: dict[str, tuple[float, float]] = {
    # (input_per_1M, output_per_1M)
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "gpt-5.4": (5.0, 20.0),
    "gpt-5.4-mini": (1.0, 4.0),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (10.0, 40.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    # ByteDance Seedance (approximate CNY→USD)
    "seedance-2.0-pro": (1.5, 6.0),
}

# ByteDance Volcano Engine ARK endpoint (OpenAI-compatible)
BYTEDANCE_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
BYTEDANCE_DEFAULT_MODEL = "seedance-2.0-pro"


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = PRICING.get(model, (5.0, 15.0))
    return (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000


@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    provider: LLMProvider


class LLMClient:
    """Unified LLM client: Claude, OpenAI, ByteDance Seedance."""

    def __init__(self):
        self._claude_client = None
        self._openai_client = None
        self._bytedance_client = None

    def _get_claude(self):
        if self._claude_client is None:
            import anthropic
            self._claude_client = anthropic.Anthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY")
            )
        return self._claude_client

    def _get_openai(self):
        if self._openai_client is None:
            import openai
            self._openai_client = openai.OpenAI(
                api_key=os.environ.get("OPENAI_API_KEY")
            )
        return self._openai_client

    def _get_bytedance(self):
        """ByteDance Seedance via Volcano Engine ARK (OpenAI-compatible API)."""
        if self._bytedance_client is None:
            import openai
            api_key = os.environ.get(
                "BYTEDANCE_API_KEY",
                "c22b4b4b-c880-4b5c-9455-d9d93e250ac5",
            )
            base_url = os.environ.get("BYTEDANCE_BASE_URL", BYTEDANCE_BASE_URL)
            self._bytedance_client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        return self._bytedance_client

    def call(
        self,
        provider: LLMProvider,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.7,
    ) -> LLMResponse:
        if provider == LLMProvider.CLAUDE:
            return self._call_claude(model, system_prompt, user_prompt, max_tokens, temperature)
        elif provider == LLMProvider.OPENAI:
            return self._call_openai(model, system_prompt, user_prompt, max_tokens, temperature)
        elif provider == LLMProvider.BYTEDANCE:
            return self._call_bytedance(model, system_prompt, user_prompt, max_tokens, temperature)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _call_claude(self, model, system_prompt, user_prompt, max_tokens, temperature):
        client = self._get_claude()
        response = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = response.content[0].text
        inp = response.usage.input_tokens
        out = response.usage.output_tokens
        return LLMResponse(content, model, inp, out,
                           estimate_cost(model, inp, out), LLMProvider.CLAUDE)

    def _call_openai(self, model, system_prompt, user_prompt, max_tokens, temperature):
        client = self._get_openai()
        response = client.chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(choice.message.content or "", model,
                           usage.prompt_tokens, usage.completion_tokens,
                           estimate_cost(model, usage.prompt_tokens, usage.completion_tokens),
                           LLMProvider.OPENAI)

    def _call_bytedance(self, model, system_prompt, user_prompt, max_tokens, temperature):
        """ByteDance Seedance 2.0 Pro via Volcano Engine ARK (OpenAI-compatible)."""
        client = self._get_bytedance()
        response = client.chat.completions.create(
            model=model or BYTEDANCE_DEFAULT_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            choice.message.content or "", model,
            usage.prompt_tokens, usage.completion_tokens,
            estimate_cost(model, usage.prompt_tokens, usage.completion_tokens),
            LLMProvider.BYTEDANCE,
        )

    def make_cost_record(self, response: LLMResponse, agent: AgentRole,
                         stage: Stage, task: str) -> CostRecord:
        return CostRecord(
            agent=agent, provider=response.provider, model=response.model,
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            cost_usd=response.cost_usd, task_description=task, stage=stage,
        )
