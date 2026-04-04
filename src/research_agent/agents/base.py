"""Base agent class — all specialized agents inherit from this.

Each agent has:
- A role (researcher, critic, engineer)
- An LLM backend configuration (provider + model)
- A system prompt template
- Stage-specific task prompts
- Output parsing logic
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Optional

import yaml

from ..integrations.llm import LLMClient, LLMResponse
from ..models import (
    AgentMessage,
    AgentRole,
    ArtifactType,
    CostRecord,
    LLMProvider,
    ProjectState,
    Stage,
)


class BaseAgent(ABC):
    """Abstract base for all agents in the research pipeline."""

    role: AgentRole
    default_provider: LLMProvider = LLMProvider.CLAUDE
    default_model: str = "claude-sonnet-4-20250514"

    def __init__(
        self,
        llm_client: LLMClient,
        provider: Optional[LLMProvider] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
    ):
        self.llm = llm_client
        self.provider = provider or self.default_provider
        self.model = model or self.default_model
        self.temperature = temperature
        self.max_tokens = max_tokens

    @abstractmethod
    def system_prompt(self, stage: Stage) -> str:
        """Return the system prompt for this agent at the given stage."""
        ...

    @abstractmethod
    def task_prompt(self, stage: Stage, context: str, instruction: str) -> str:
        """Build the user prompt for a specific task."""
        ...

    @abstractmethod
    def expected_output_type(self, stage: Stage) -> Optional[ArtifactType]:
        """What artifact type this agent produces at this stage, if any."""
        ...

    def execute(
        self,
        stage: Stage,
        context: str,
        instruction: str,
        state: ProjectState,
    ) -> tuple[str, LLMResponse]:
        """Execute the agent: build prompts, call LLM, return (content, response)."""
        sys_prompt = self.system_prompt(stage)
        user_prompt = self.task_prompt(stage, context, instruction)

        response = self.llm.call(
            provider=self.provider,
            model=self.model,
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        # Record cost
        cost = self.llm.make_cost_record(response, self.role, stage, instruction[:100])
        state.cost_records.append(cost)

        # Record message
        msg = AgentMessage(
            sender=self.role,
            receiver=AgentRole.ORCHESTRATOR,
            content=response.content[:500],  # Summary only in state
            stage=stage,
        )
        state.messages.append(msg)

        return response.content, response

    def extract_yaml_block(self, text: str) -> Optional[str]:
        """Extract the first YAML code block from agent output."""
        pattern = r"```ya?ml\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Try to parse the whole thing as YAML if no code block
        try:
            yaml.safe_load(text)
            return text.strip()
        except yaml.YAMLError:
            return None

    def extract_markdown_block(self, text: str) -> Optional[str]:
        """Extract the first markdown code block from agent output."""
        pattern = r"```(?:markdown|md)?\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()
