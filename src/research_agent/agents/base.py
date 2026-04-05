"""Base agent class — prompt templates and output type declarations.

All actual execution goes through CLI dispatch (claude/codex/opencode).
Agent classes define system prompts, task prompts, and expected output types.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

import yaml

from ..models import AgentRole, ArtifactType, Stage


class BaseAgent(ABC):
    """Abstract base for all agents in the research pipeline."""

    role: AgentRole

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

    def extract_yaml_block(self, text: str) -> Optional[str]:
        """Extract the first YAML code block from agent output."""
        pattern = r"```ya?ml\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        try:
            yaml.safe_load(text)
            return text.strip()
        except yaml.YAMLError:
            return None
