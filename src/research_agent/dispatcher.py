"""Multi-agent dispatcher — launches separate Claude Code instances per role.

Each agent is a standalone `claude -p` process with:
- Role-specific system prompt (from agents/<role>/CLAUDE.md)
- Restricted tool set (Researcher can't run Bash, Engineer can't WebSearch)
- Structured task card as input (YAML)
- Structured output parsed from agent response

Communication is via the shared filesystem:
  projects/<id>/artifacts/    ← shared artifact store
  projects/<id>/task_cards/   ← task cards (input/output per agent)
  projects/<id>/state.json    ← pipeline state (only Orchestrator writes)

This replaces the single-Claude-Code model where one session did everything.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import AgentRole, CostRecord, LLMProvider, ProjectState, Stage


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

class AgentToolset(str, Enum):
    """Predefined tool sets per role — enforce separation of concerns."""
    RESEARCHER = "Read,Write,Glob,Grep,WebSearch,WebFetch"
    ENGINEER = "Read,Write,Edit,Bash,Glob,Grep"
    ORCHESTRATOR = "Read,Write,Bash,Glob,Grep"


# Claude Code model to use for each agent (configurable)
DEFAULT_AGENT_MODELS: dict[AgentRole, str] = {
    AgentRole.RESEARCHER: "claude-sonnet-4-20250514",
    AgentRole.ENGINEER: "claude-sonnet-4-20250514",
    AgentRole.ORCHESTRATOR: "claude-sonnet-4-20250514",
}

# Max turns per agent invocation (prevents runaway sessions)
DEFAULT_MAX_TURNS: dict[AgentRole, int] = {
    AgentRole.RESEARCHER: 15,
    AgentRole.ENGINEER: 25,
    AgentRole.ORCHESTRATOR: 10,
}


@dataclass
class TaskCard:
    """Structured input for an agent — replaces free-form chat."""
    task_id: str
    role: AgentRole
    stage: Stage
    instruction: str
    context_files: list[str] = field(default_factory=list)  # Paths to read
    required_outputs: list[str] = field(default_factory=list)  # Expected output files
    previous_feedback: str = ""  # Gate feedback from prior iteration
    constraints: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> str:
        return yaml.dump({
            "task_id": self.task_id,
            "role": self.role.value,
            "stage": self.stage.value,
            "instruction": self.instruction,
            "context_files": self.context_files,
            "required_outputs": self.required_outputs,
            "previous_feedback": self.previous_feedback,
            "constraints": self.constraints,
            "metadata": self.metadata,
        }, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, content: str) -> "TaskCard":
        data = yaml.safe_load(content)
        return cls(
            task_id=data["task_id"],
            role=AgentRole(data["role"]),
            stage=Stage(data["stage"]),
            instruction=data["instruction"],
            context_files=data.get("context_files", []),
            required_outputs=data.get("required_outputs", []),
            previous_feedback=data.get("previous_feedback", ""),
            constraints=data.get("constraints", []),
            metadata=data.get("metadata", {}),
        )


@dataclass
class AgentResult:
    """Structured output from an agent run."""
    task_id: str
    role: AgentRole
    success: bool
    output_text: str
    output_files: list[str] = field(default_factory=list)  # Files the agent created/modified
    error: str = ""
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class MultiAgentDispatcher:
    """Launches and manages multiple Claude Code instances as separate agents."""

    def __init__(
        self,
        project_dir: Path,
        agents_dir: Path,
        config: dict[str, Any] = None,
    ):
        self.project_dir = project_dir
        self.agents_dir = agents_dir
        self.config = config or {}

        # Per-agent model overrides from config
        agent_cfg = self.config.get("agents", {})
        self.models: dict[AgentRole, str] = {
            AgentRole.RESEARCHER: agent_cfg.get("researcher", {}).get(
                "model", DEFAULT_AGENT_MODELS[AgentRole.RESEARCHER]
            ),
            AgentRole.ENGINEER: agent_cfg.get("engineer", {}).get(
                "model", DEFAULT_AGENT_MODELS[AgentRole.ENGINEER]
            ),
            AgentRole.ORCHESTRATOR: agent_cfg.get("orchestrator", {}).get(
                "model", DEFAULT_AGENT_MODELS[AgentRole.ORCHESTRATOR]
            ),
        }
        self.max_turns: dict[AgentRole, int] = {
            AgentRole.RESEARCHER: agent_cfg.get("researcher", {}).get(
                "max_turns", DEFAULT_MAX_TURNS[AgentRole.RESEARCHER]
            ),
            AgentRole.ENGINEER: agent_cfg.get("engineer", {}).get(
                "max_turns", DEFAULT_MAX_TURNS[AgentRole.ENGINEER]
            ),
            AgentRole.ORCHESTRATOR: agent_cfg.get("orchestrator", {}).get(
                "max_turns", DEFAULT_MAX_TURNS[AgentRole.ORCHESTRATOR]
            ),
        }

    def dispatch(self, task: TaskCard) -> AgentResult:
        """Dispatch a task to the appropriate Claude Code agent.

        1. Load role-specific CLAUDE.md
        2. Build prompt from task card + context files
        3. Launch `claude -p` with restricted tools
        4. Parse output and collect artifacts
        """
        role = task.role
        if role == AgentRole.CRITIC:
            return self._dispatch_codex(task)

        prompt = self._build_prompt(task)
        toolset = self._get_toolset(role)
        model = self.models.get(role, DEFAULT_AGENT_MODELS.get(role, "claude-sonnet-4-20250514"))
        max_turns = self.max_turns.get(role, 15)

        start = time.time()
        output, exit_code = self._run_claude(prompt, toolset, model, max_turns)
        duration = time.time() - start

        # Detect output files (agent should mention them)
        output_files = self._detect_output_files(task, output)

        success = exit_code == 0 and not any(
            err in output.lower() for err in ["error:", "failed:", "exception:"]
        )

        return AgentResult(
            task_id=task.task_id,
            role=role,
            success=success,
            output_text=output,
            output_files=output_files,
            error="" if success else f"Exit code: {exit_code}",
            duration_seconds=duration,
            exit_code=exit_code,
        )

    def dispatch_parallel(self, tasks: list[TaskCard]) -> list[AgentResult]:
        """Dispatch multiple tasks in parallel (different agents simultaneously)."""
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {executor.submit(self.dispatch, task): task for task in tasks}
            results = []
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        return results

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _build_prompt(self, task: TaskCard) -> str:
        """Build the full prompt for a Claude Code agent."""
        parts: list[str] = []

        # 1. Role-specific instructions from CLAUDE.md
        role_instructions = self._load_role_instructions(task.role)
        if role_instructions:
            parts.append(role_instructions)
            parts.append("")

        # 2. Task card
        parts.append("## Task Card")
        parts.append(f"Task ID: {task.task_id}")
        parts.append(f"Stage: {task.stage.value}")
        parts.append(f"Instruction: {task.instruction}")
        parts.append("")

        if task.constraints:
            parts.append("## Constraints")
            for c in task.constraints:
                parts.append(f"- {c}")
            parts.append("")

        if task.required_outputs:
            parts.append("## Required Outputs")
            parts.append("You MUST produce these files:")
            for f in task.required_outputs:
                parts.append(f"- {f}")
            parts.append("")

        # 3. Context files — instruct agent to read them
        if task.context_files:
            parts.append("## Context Files")
            parts.append("Read these files for context before starting:")
            for f in task.context_files:
                parts.append(f"- {f}")
            parts.append("")

        # 4. Previous feedback (for iterations)
        if task.previous_feedback:
            parts.append("## Previous Review Feedback (MUST ADDRESS)")
            parts.append(task.previous_feedback)
            parts.append("")

        # 5. Output instructions
        parts.append("## Output Instructions")
        parts.append(
            "Write your output artifacts to the specified file paths. "
            "When done, print a YAML summary block wrapped in ```yaml ... ``` "
            "with fields: status (done/blocked), files_written (list), notes (string)."
        )

        return "\n".join(parts)

    def _load_role_instructions(self, role: AgentRole) -> str:
        """Load role-specific CLAUDE.md."""
        role_dir = self.agents_dir / role.value
        claude_md = role_dir / "CLAUDE.md"
        if claude_md.exists():
            return claude_md.read_text(encoding="utf-8")
        return ""

    def _get_toolset(self, role: AgentRole) -> str:
        """Get allowed tools for a role."""
        role_to_toolset = {
            AgentRole.RESEARCHER: AgentToolset.RESEARCHER.value,
            AgentRole.ENGINEER: AgentToolset.ENGINEER.value,
            AgentRole.ORCHESTRATOR: AgentToolset.ORCHESTRATOR.value,
        }
        return role_to_toolset.get(role, AgentToolset.ENGINEER.value)

    def _run_claude(
        self,
        prompt: str,
        allowed_tools: str,
        model: str,
        max_turns: int,
    ) -> tuple[str, int]:
        """Launch a Claude Code process via stdin pipe."""
        cmd = [
            "claude",
            "-p",
            "--output-format", "text",
            "--model", model,
            "--allowedTools", allowed_tools,
        ]

        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(self.project_dir),
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max per agent
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "agent-dispatch"},
        )

        output = result.stdout
        if result.returncode != 0 and not output.strip():
            output = result.stderr

        return output, result.returncode

    def _dispatch_codex(self, task: TaskCard) -> AgentResult:
        """Dispatch to Codex (critic role)."""
        from .integrations.codex import codex_review
        from .agents.critic import STAGE_REVIEW_CRITERIA

        # Read context files
        context_parts = []
        for f in task.context_files:
            p = self.project_dir / f
            if p.exists():
                context_parts.append(f"## {p.name}\n{p.read_text()}")

        criteria = STAGE_REVIEW_CRITERIA.get(task.stage.value, "Review for rigor.")

        start = time.time()
        result = codex_review(
            stage=task.stage.value,
            artifact_content="\n\n".join(context_parts),
            review_criteria=criteria,
            project_context=task.instruction,
            model=self.config.get("agents", {}).get("critic", {}).get("model", "gpt-5.4"),
            effort=self.config.get("agents", {}).get("critic", {}).get("effort", "xhigh"),
            project_dir=self.project_dir,
        )
        duration = time.time() - start

        # Save review as file
        review_path = self.project_dir / "projects" / task.metadata.get("project_id", "default")
        review_path = review_path / "artifacts" / task.stage.value / f"review_{task.task_id}.yaml"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(yaml.dump({
            "verdict": result.verdict,
            "scores": result.scores,
            "blocking_issues": result.blocking_issues,
            "suggestions": result.suggestions,
            "strongest_objection": result.strongest_objection,
            "what_would_make_it_pass": result.what_would_make_it_pass,
        }, default_flow_style=False, allow_unicode=True))

        return AgentResult(
            task_id=task.task_id,
            role=AgentRole.CRITIC,
            success=result.verdict == "PASS",
            output_text=result.raw_output,
            output_files=[str(review_path)],
            duration_seconds=duration,
        )

    def _detect_output_files(self, task: TaskCard, output: str) -> list[str]:
        """Detect files the agent wrote by checking required_outputs existence."""
        found = []
        for expected in task.required_outputs:
            p = self.project_dir / expected
            if p.exists():
                found.append(expected)
        return found
