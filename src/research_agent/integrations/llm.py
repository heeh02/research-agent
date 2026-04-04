"""LLM provider abstraction — supports Claude, OpenAI, and extensible to local models.

Key design: different agents can use different models. The Researcher might use Claude
for its strong synthesis ability, while the Critic uses GPT for independent perspective.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..models import CostRecord, LLMProvider, AgentRole, Stage


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens, approximate as of 2025-Q4)
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
    "o3-mini": (1.10, 4.40),
    "o3": (10.0, 40.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = PRICING.get(model, (5.0, 15.0))  # default fallback
    return (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000


# ---------------------------------------------------------------------------
# Response container
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    provider: LLMProvider


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class LLMClient:
    """Unified LLM client that dispatches to the right provider."""

    def __init__(self):
        self._claude_client = None
        self._openai_client = None

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
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _call_claude(self, model: str, system_prompt: str, user_prompt: str,
                     max_tokens: int, temperature: float) -> LLMResponse:
        client = self._get_claude()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = response.content[0].text
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        return LLMResponse(
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(model, input_tokens, output_tokens),
            provider=LLMProvider.CLAUDE,
        )

    def _call_openai(self, model: str, system_prompt: str, user_prompt: str,
                     max_tokens: int, temperature: float) -> LLMResponse:
        client = self._get_openai()
        response = client.chat.completions.create(
            model=model,
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
            content=choice.message.content or "",
            model=model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost_usd=estimate_cost(model, usage.prompt_tokens, usage.completion_tokens),
            provider=LLMProvider.OPENAI,
        )

    def make_cost_record(
        self, response: LLMResponse, agent: AgentRole, stage: Stage, task: str,
    ) -> CostRecord:
        return CostRecord(
            agent=agent,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            task_description=task,
            stage=stage,
        )
