"""Multi-agent dispatcher — launches CLI tools (claude / codex / opencode) per role.

Three CLI backends:
  - claude:   Claude Code CLI (`claude -p`)
  - codex:    OpenAI Codex CLI (`codex exec`)
  - opencode: OpenCode CLI (`opencode run`) — supports Doubao, DeepSeek, Kimi, etc.

Each agent runs as an independent subprocess with role-specific prompt and tools.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import AgentRole, CLIBackend, CostRecord, LLMProvider, ProjectState, Stage


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

class AgentToolset(str, Enum):
    RESEARCHER = "Read,Write,Glob,Grep,WebSearch,WebFetch,Agent"
    ENGINEER = "Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent"
    ORCHESTRATOR = "Read,Write,Bash,Glob,Grep"


DEFAULT_AGENT_MODELS: dict[AgentRole, str] = {
    AgentRole.RESEARCHER: "claude-sonnet-4-20250514",
    AgentRole.ENGINEER: "claude-sonnet-4-20250514",
    AgentRole.ORCHESTRATOR: "claude-sonnet-4-20250514",
}

DEFAULT_AGENT_EFFORT: dict[AgentRole, str] = {
    AgentRole.RESEARCHER: "max",
    AgentRole.ENGINEER: "high",
    AgentRole.ORCHESTRATOR: "medium",
}

DEFAULT_MAX_TURNS: dict[AgentRole, int] = {
    AgentRole.RESEARCHER: 30,
    AgentRole.ENGINEER: 40,
    AgentRole.ORCHESTRATOR: 10,
}

# OpenCode binary path (user-installed)
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", os.path.expanduser("~/.opencode/bin/opencode"))

# Errors that are transient and should be retried
RETRYABLE_PATTERNS = [
    "403", "api error", "please run /login", "rate limit", "overloaded",
    "connection reset", "connection refused", "timed out", "timeout",
    "network", "eof", "broken pipe", "502", "503", "529",
]


@dataclass
class TaskCard:
    task_id: str
    role: AgentRole
    stage: Stage
    instruction: str
    context_files: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    previous_feedback: str = ""
    constraints: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> str:
        return yaml.dump({
            "task_id": self.task_id, "role": self.role.value,
            "stage": self.stage.value, "instruction": self.instruction,
            "context_files": self.context_files,
            "required_outputs": self.required_outputs,
            "previous_feedback": self.previous_feedback,
            "constraints": self.constraints,
            "metadata": self.metadata,
        }, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, content: str) -> "TaskCard":
        d = yaml.safe_load(content)
        return cls(
            task_id=d["task_id"], role=AgentRole(d["role"]), stage=Stage(d["stage"]),
            instruction=d["instruction"], context_files=d.get("context_files", []),
            required_outputs=d.get("required_outputs", []),
            previous_feedback=d.get("previous_feedback", ""),
            constraints=d.get("constraints", []),
            metadata=d.get("metadata", {}),
        )


@dataclass
class AgentResult:
    task_id: str
    role: AgentRole
    success: bool
    output_text: str
    output_files: list[str] = field(default_factory=list)
    error: str = ""
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    exit_code: int = 0
    retries: int = 0
    is_auth_error: bool = False


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _is_retryable(output: str, exit_code: int) -> bool:
    if exit_code == 0:
        return False
    combined = output.lower()
    return any(pat in combined for pat in RETRYABLE_PATTERNS)


def _is_auth_error(output: str) -> bool:
    lower = output.lower()
    return "403" in lower or "/login" in lower or "please run /login" in lower


def _retry_wait(attempt: int) -> float:
    return min(10 * (3 ** attempt), 300)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class MultiAgentDispatcher:
    """Launches and manages CLI tool instances as separate agents.

    Supports three backends: claude (Claude Code), codex (Codex), opencode (OpenCode).
    """

    def __init__(
        self,
        project_dir: Path,
        agents_dir: Path,
        config: dict[str, Any] | None = None,
        max_retries: int = 5,
    ):
        self.project_dir = project_dir
        self.agents_dir = agents_dir
        self.config = config or {}
        self.max_retries = max_retries

        agent_cfg = self.config.get("agents", {})

        # Parse per-role CLI backend
        self.backends: dict[AgentRole, CLIBackend] = {}
        for role in AgentRole:
            raw = agent_cfg.get(role.value, {}).get("backend", None)
            if raw:
                self.backends[role] = CLIBackend(raw)
            else:
                # Default: critic→codex, others→claude
                self.backends[role] = CLIBackend.CODEX if role == AgentRole.CRITIC else CLIBackend.CLAUDE

        self.models: dict[AgentRole, str] = {
            AgentRole.RESEARCHER: agent_cfg.get("researcher", {}).get("model", DEFAULT_AGENT_MODELS[AgentRole.RESEARCHER]),
            AgentRole.ENGINEER: agent_cfg.get("engineer", {}).get("model", DEFAULT_AGENT_MODELS[AgentRole.ENGINEER]),
            AgentRole.CRITIC: agent_cfg.get("critic", {}).get("model", "gpt-5.4"),
            AgentRole.ORCHESTRATOR: agent_cfg.get("orchestrator", {}).get("model", DEFAULT_AGENT_MODELS[AgentRole.ORCHESTRATOR]),
        }
        self.effort: dict[AgentRole, str] = {
            AgentRole.RESEARCHER: agent_cfg.get("researcher", {}).get("effort", DEFAULT_AGENT_EFFORT[AgentRole.RESEARCHER]),
            AgentRole.ENGINEER: agent_cfg.get("engineer", {}).get("effort", DEFAULT_AGENT_EFFORT[AgentRole.ENGINEER]),
            AgentRole.ORCHESTRATOR: agent_cfg.get("orchestrator", {}).get("effort", DEFAULT_AGENT_EFFORT[AgentRole.ORCHESTRATOR]),
        }
        self.max_turns: dict[AgentRole, int] = {
            AgentRole.RESEARCHER: agent_cfg.get("researcher", {}).get("max_turns", DEFAULT_MAX_TURNS[AgentRole.RESEARCHER]),
            AgentRole.ENGINEER: agent_cfg.get("engineer", {}).get("max_turns", DEFAULT_MAX_TURNS[AgentRole.ENGINEER]),
            AgentRole.ORCHESTRATOR: agent_cfg.get("orchestrator", {}).get("max_turns", DEFAULT_MAX_TURNS[AgentRole.ORCHESTRATOR]),
        }

    def dispatch(self, task: TaskCard) -> AgentResult:
        """Dispatch task to the appropriate CLI backend with retry."""
        role = task.role
        backend = self.backends.get(role, CLIBackend.CLAUDE)

        if backend == CLIBackend.CODEX:
            return self._dispatch_codex_with_retry(task)

        # Claude and OpenCode share the same retry loop
        prompt = self._build_prompt(task)
        toolset = self._get_toolset(role)
        model = self.models.get(role, DEFAULT_AGENT_MODELS.get(role, "claude-sonnet-4-20250514"))
        effort = self.effort.get(role, DEFAULT_AGENT_EFFORT.get(role, "high"))

        last_result = None
        for attempt in range(self.max_retries + 1):
            start = time.time()
            try:
                if backend == CLIBackend.OPENCODE:
                    output, exit_code = self._run_opencode(
                        prompt, model, effort,
                        expected_files=task.required_outputs,
                    )
                else:
                    output, exit_code = self._run_claude(prompt, toolset, model, effort)
            except subprocess.TimeoutExpired:
                output = f"ERROR: {backend.value} process timed out"
                exit_code = 124
            except Exception as e:
                output = f"ERROR: {type(e).__name__}: {e}"
                exit_code = 1
            duration = time.time() - start

            if exit_code != 0 and _is_retryable(output, exit_code) and attempt < self.max_retries:
                wait = _retry_wait(attempt)
                auth = _is_auth_error(output)
                if auth:
                    print(f"    ⚠ Auth error. Waiting {wait:.0f}s then retrying...")
                else:
                    print(f"    ⚠ Transient error (attempt {attempt+1}/{self.max_retries+1}). Retrying in {wait:.0f}s...")
                    print(f"    ⚠ Error: {output[:150]}")
                time.sleep(wait)
                continue

            output_files = self._detect_output_files(task, output)
            success = exit_code == 0 and bool(output_files)

            self._save_full_log(task, output)

            last_result = AgentResult(
                task_id=task.task_id, role=role, success=success,
                output_text=output, output_files=output_files,
                error="" if success else f"Exit code: {exit_code}",
                duration_seconds=duration, exit_code=exit_code,
                retries=attempt, is_auth_error=_is_auth_error(output),
            )
            break

        return last_result or AgentResult(
            task_id=task.task_id, role=role, success=False,
            output_text="All retries exhausted", error="Max retries exceeded",
            retries=self.max_retries, is_auth_error=True,
        )

    def dispatch_parallel(self, tasks: list[TaskCard]) -> list[AgentResult]:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {executor.submit(self.dispatch, t): t for t in tasks}
            return [f.result() for f in concurrent.futures.as_completed(futures)]

    # -----------------------------------------------------------------------
    # Internal — Claude Code CLI
    # -----------------------------------------------------------------------

    def _build_prompt(self, task: TaskCard) -> str:
        parts: list[str] = []

        role_instructions = self._load_role_instructions(task.role)
        if role_instructions:
            parts.append(role_instructions)
            parts.append("")

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

        if task.context_files:
            parts.append("## Context Files")
            parts.append("Read these files for context before starting:")
            for f in task.context_files:
                parts.append(f"- {f}")
            parts.append("")

        if task.previous_feedback:
            parts.append("## Previous Review Feedback (MUST ADDRESS)")
            parts.append(task.previous_feedback)
            parts.append("")

        parts.append("## Output Instructions")
        parts.append(
            "Write your output artifacts to the specified file paths. "
            "When done, print a YAML summary block wrapped in ```yaml ... ``` "
            "with fields: status (done/blocked), files_written (list), notes (string)."
        )
        return "\n".join(parts)

    def _load_role_instructions(self, role: AgentRole) -> str:
        claude_md = self.agents_dir / role.value / "CLAUDE.md"
        return claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""

    def _get_toolset(self, role: AgentRole) -> str:
        """Get allowed tools: config-driven with enum defaults."""
        agent_cfg = self.config.get("agents", {})
        role_cfg = agent_cfg.get(role.value, {})
        if "allowed_tools" in role_cfg:
            return role_cfg["allowed_tools"]
        return {
            AgentRole.RESEARCHER: AgentToolset.RESEARCHER.value,
            AgentRole.ENGINEER: AgentToolset.ENGINEER.value,
            AgentRole.ORCHESTRATOR: AgentToolset.ORCHESTRATOR.value,
        }.get(role, AgentToolset.ENGINEER.value)

    def _run_claude(self, prompt: str, allowed_tools: str, model: str,
                    effort: str = "high") -> tuple[str, int]:
        cmd = [
            "claude", "-p",
            "--output-format", "text",
            "--model", model,
            "--effort", effort,
            "--allowedTools", allowed_tools,
        ]
        timeout = 900 if effort == "max" else 600
        result = subprocess.run(
            cmd, input=prompt,
            cwd=str(self.project_dir),
            capture_output=True, text=True,
            timeout=timeout,
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "agent-dispatch"},
        )
        output = result.stdout
        if result.returncode != 0 and not output.strip():
            output = result.stderr
        return output, result.returncode

    # -----------------------------------------------------------------------
    # Internal — OpenCode CLI
    # -----------------------------------------------------------------------

    def _run_opencode(self, prompt: str, model: str,
                      effort: str = "high",
                      expected_files: list[str] | None = None,
                      ) -> tuple[str, int]:
        """Run opencode run with a prompt. Returns (output, exit_code).

        Key design: opencode starts a local server and may NOT exit after
        completing a task. We use Popen + file-based completion detection:
        1. Start process in its own session (so we can kill the whole tree)
        2. Pipe the prompt via stdin (avoids CLI arg length limits)
        3. Poll for expected output files on disk
        4. When files appear (or timeout), kill the process group
        5. Capture whatever stdout was produced
        """
        cmd = [
            OPENCODE_BIN, "run",
            "--dir", str(self.project_dir),
        ]

        if model:
            cmd.extend(["-m", model])

        if effort and effort != "none":
            cmd.extend(["--variant", effort])

        # Pass prompt as the message argument (not via stdin, as opencode
        # expects the message as positional args)
        cmd.append(prompt)

        timeout = 900 if effort == "max" else 600

        # Start in new session so we can kill the entire process tree
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        # Background thread to drain stdout (prevents pipe buffer deadlock)
        output_lines: list[str] = []
        def _drain():
            try:
                for line in proc.stdout:
                    output_lines.append(line)
            except Exception:
                pass

        import threading
        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()

        # Poll for completion: either process exits or expected files appear
        start = time.time()
        files_found = False
        poll_interval = 5  # seconds between checks

        while time.time() - start < timeout:
            # Check if process exited naturally
            if proc.poll() is not None:
                break

            # Check if expected output files exist on disk
            if expected_files:
                all_exist = all(
                    (self.project_dir / f).exists() for f in expected_files
                )
                if all_exist:
                    # Files written! Give opencode a few seconds to finish cleanup
                    files_found = True
                    time.sleep(3)
                    break

            time.sleep(poll_interval)

        # Collect output and kill process tree
        exit_code = proc.poll()
        if exit_code is None:
            # Process still running — kill entire process group
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            exit_code = 0 if files_found else 124  # 124 = timeout

        drain_thread.join(timeout=5)
        output = "".join(output_lines)

        if not output.strip() and files_found:
            output = f"Files produced: {expected_files}"

        return output, exit_code

    # -----------------------------------------------------------------------
    # Internal — Codex CLI (with retry)
    # -----------------------------------------------------------------------

    def _dispatch_codex_with_retry(self, task: TaskCard) -> AgentResult:
        for attempt in range(self.max_retries + 1):
            try:
                result = self._dispatch_codex(task)
                if not result.success and _is_retryable(result.output_text, 1) and attempt < self.max_retries:
                    wait = _retry_wait(attempt)
                    print(f"    ⚠ Codex transient error (attempt {attempt+1}). Retrying in {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                result.retries = attempt
                return result
            except (subprocess.TimeoutExpired, OSError) as e:
                if attempt < self.max_retries:
                    wait = _retry_wait(attempt)
                    print(f"    ⚠ Codex error: {e}. Retrying in {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                return AgentResult(
                    task_id=task.task_id, role=AgentRole.CRITIC,
                    success=False, output_text=f"Codex failed: {e}",
                    error=str(e), retries=attempt,
                )

        return AgentResult(
            task_id=task.task_id, role=AgentRole.CRITIC,
            success=False, output_text="Codex: all retries exhausted",
            retries=self.max_retries,
        )

    def _dispatch_codex(self, task: TaskCard) -> AgentResult:
        from .integrations.codex import codex_review
        from .agents.critic import STAGE_REVIEW_CRITERIA

        context_parts = []
        for f in task.context_files:
            p = self.project_dir / f
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                context_parts.append(self._yaml_to_readable(p.name, raw))

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

        pid = task.metadata.get("project_id", "default")
        base = self.project_dir / "projects" / pid

        review_path = base / "artifacts" / task.stage.value / f"review_{task.task_id}.yaml"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_data = {
            "verdict": result.verdict,
            "scores": result.scores,
            "blocking_issues": result.blocking_issues,
            "suggestions": result.suggestions,
            "strongest_objection": result.strongest_objection,
            "what_would_make_it_pass": result.what_would_make_it_pass,
        }
        review_path.write_text(
            yaml.dump(review_data, default_flow_style=False, allow_unicode=True, width=120),
            encoding="utf-8",
        )

        self._save_full_log(task, result.raw_output)

        return AgentResult(
            task_id=task.task_id, role=AgentRole.CRITIC,
            success=result.verdict == "PASS",
            output_text=result.raw_output,
            output_files=[str(review_path)],
            duration_seconds=duration,
        )

    # -----------------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------------

    def _detect_output_files(self, task: TaskCard, output: str) -> list[str]:
        found = set()
        for expected in task.required_outputs:
            if (self.project_dir / expected).exists():
                found.add(expected)

        pid = task.metadata.get("project_id", "")
        if pid:
            art_dir = self.project_dir / "projects" / pid / "artifacts" / task.stage.value
            if art_dir.exists():
                for f in art_dir.glob("*.yaml"):
                    if not f.name.startswith("review_"):
                        rel = f"projects/{pid}/artifacts/{task.stage.value}/{f.name}"
                        found.add(rel)

        return list(found)

    def _save_full_log(self, task: TaskCard, output: str) -> Path:
        pid = task.metadata.get("project_id", "default")
        log_dir = self.project_dir / "projects" / pid / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task.task_id}.txt"
        log_file.write_text(output, encoding="utf-8")
        return log_file

    @staticmethod
    def _yaml_to_readable(filename: str, raw_yaml: str) -> str:
        try:
            data = yaml.safe_load(raw_yaml)
        except yaml.YAMLError:
            return f"## {filename}\n{raw_yaml}"

        if not isinstance(data, dict):
            return f"## {filename}\n{raw_yaml}"

        lines = [f"## {filename}\n"]
        for key, value in data.items():
            heading = key.replace("_", " ").title()
            if isinstance(value, str):
                lines.append(f"### {heading}\n{value}\n")
            elif isinstance(value, list):
                lines.append(f"### {heading}")
                for i, item in enumerate(value, 1):
                    if isinstance(item, dict):
                        parts = [f"{k}: {v}" for k, v in item.items()]
                        lines.append(f"  {i}. " + " | ".join(parts))
                    else:
                        lines.append(f"  {i}. {item}")
                lines.append("")
            elif isinstance(value, dict):
                lines.append(f"### {heading}")
                for k, v in value.items():
                    lines.append(f"  - {k}: {v}")
                lines.append("")
            else:
                lines.append(f"### {heading}\n{value}\n")

        return "\n".join(lines)
