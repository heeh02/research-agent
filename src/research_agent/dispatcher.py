"""Multi-agent dispatcher — launches CLI tools (claude / codex / opencode) per role.

Three CLI backends:
  - claude:   Claude Code CLI (`claude -p`)
  - codex:    OpenAI Codex CLI (`codex exec`)
  - opencode: OpenCode CLI (`opencode run`) — supports Doubao, DeepSeek, Kimi, etc.

Each agent runs as an independent subprocess with role-specific prompt and tools.

On macOS, agents can optionally run in visible Terminal.app windows so the user
can watch each CLI tool in real-time.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import (
    AgentRole, ArtifactType, CLIBackend, CostRecord, LLMProvider,
    ProjectState, Stage, STAGE_REQUIRED_ARTIFACTS,
)
from .sandbox import (
    FileSnapshot,
    ViolationReport,
    check_violations,
    snapshot_directory,
)


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

class AgentToolset(str, Enum):
    RESEARCHER = "Read,Write,Glob,Grep,WebSearch,WebFetch,Agent"
    ENGINEER = "Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent"
    ORCHESTRATOR = "Read,Write,Bash,Glob,Grep"


DEFAULT_AGENT_MODELS: dict[AgentRole, str] = {
    AgentRole.RESEARCHER: "claude-sonnet-4-6",
    AgentRole.ENGINEER: "claude-sonnet-4-6",
    AgentRole.ORCHESTRATOR: "claude-sonnet-4-6",
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
    input_tokens: int = 0
    output_tokens: int = 0
    cost_source: str = "unknown"  # "claude_cli", "estimated", "unknown"
    exit_code: int = 0
    retries: int = 0
    is_auth_error: bool = False
    violations: list[str] = field(default_factory=list)  # Isolation violation paths


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
        self._active_proc = None  # Track running subprocess for force-kill
        self._terminal_pid_file: str | None = None  # PID file for visible terminal process
        # Open CLI tools in visible Terminal.app windows (macOS only)
        self.visible_terminal: bool = (
            self.config.get("gui", {}).get("visible_terminal", True)
            and platform.system() == "Darwin"
        )
        self.max_turns: dict[AgentRole, int] = {
            AgentRole.RESEARCHER: agent_cfg.get("researcher", {}).get("max_turns", DEFAULT_MAX_TURNS[AgentRole.RESEARCHER]),
            AgentRole.ENGINEER: agent_cfg.get("engineer", {}).get("max_turns", DEFAULT_MAX_TURNS[AgentRole.ENGINEER]),
            AgentRole.ORCHESTRATOR: agent_cfg.get("orchestrator", {}).get("max_turns", DEFAULT_MAX_TURNS[AgentRole.ORCHESTRATOR]),
        }

    def dispatch(self, task: TaskCard, progress_fn=None, cancel_event=None) -> AgentResult:
        """Dispatch task to the appropriate CLI backend with retry.

        Args:
            progress_fn: Optional callback(str) for progress messages.
            cancel_event: Optional threading.Event — when set, abort immediately.
        """
        role = task.role
        backend = self.backends.get(role, CLIBackend.CLAUDE)

        if backend == CLIBackend.CODEX:
            return self._dispatch_codex_with_retry(task)

        # Claude and OpenCode share the same retry loop
        prompt = self._build_prompt(task)
        toolset = self._get_toolset(role)
        model = self.models.get(role, DEFAULT_AGENT_MODELS.get(role, "claude-sonnet-4-6"))
        effort = self.effort.get(role, DEFAULT_AGENT_EFFORT.get(role, "high"))

        # Critic doesn't produce files — it outputs verdict in stdout
        is_critic = (role == AgentRole.CRITIC)
        if is_critic:
            # Don't wait for file creation; critic output is in the text
            task = TaskCard(
                task_id=task.task_id, role=task.role, stage=task.stage,
                instruction=task.instruction, context_files=task.context_files,
                required_outputs=[],  # Critic writes NO files
                previous_feedback=task.previous_feedback,
                constraints=task.constraints, metadata=task.metadata,
            )

        # Snapshot project dir before dispatch (for violation detection)
        pid = task.metadata.get("project_id", "")
        snap_before: FileSnapshot | None = None
        if pid:
            snap_before = snapshot_directory(self.project_dir, pid)

        last_result = None
        for attempt in range(self.max_retries + 1):
            cost_usd = 0.0
            in_tokens = 0
            out_tokens = 0
            cost_source = "unknown"

            start = time.time()
            try:
                if backend == CLIBackend.OPENCODE:
                    output, exit_code = self._run_opencode(
                        prompt, model, effort,
                        expected_files=task.required_outputs,
                        progress_fn=progress_fn,
                        cancel_event=cancel_event,
                    )
                    # OpenCode has no usage data — estimate from text length
                    cost_usd, in_tokens, out_tokens = self._estimate_cost_from_text(
                        prompt, output, model)
                    cost_source = "estimated"
                else:
                    output, exit_code, cost_usd, in_tokens, out_tokens = self._run_claude(
                        prompt, toolset, model, effort)
                    cost_source = "claude_cli" if cost_usd > 0 else "unknown"
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

            output_files = self._detect_output_files(task, output, dispatch_start=start)
            # Critic success = process completed (NOT verdict approval).
            # Verdict interpretation belongs in the caller (run_review / _do_review).
            if is_critic:
                success = exit_code == 0 or bool(output.strip())
            else:
                success = exit_code == 0 and bool(output_files)

            # Check for isolation violations BEFORE saving our own log
            # (the log is written by the orchestrator, not the agent)
            violation_paths: list[str] = []
            if snap_before and pid:
                snap_after = snapshot_directory(self.project_dir, pid)
                vr = check_violations(
                    snap_before, snap_after, role, task.stage,
                    task.required_outputs, pid,
                )
                violation_paths = [v.path for v in vr.violations]

            self._save_full_log(task, output)

            last_result = AgentResult(
                task_id=task.task_id, role=role, success=success,
                output_text=output, output_files=output_files,
                error="" if success else f"Exit code: {exit_code}",
                duration_seconds=duration, cost_usd=cost_usd,
                input_tokens=in_tokens, output_tokens=out_tokens,
                cost_source=cost_source,
                exit_code=exit_code,
                retries=attempt, is_auth_error=_is_auth_error(output),
                violations=violation_paths,
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
    # Cost parsing helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_claude_json(raw_json: str) -> tuple[str, int, float, int, int]:
        """Parse Claude CLI JSON output.

        Returns (result_text, exit_code_hint, cost_usd, input_tokens, output_tokens).
        If parsing fails, returns the raw text as result with zero cost.
        """
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError):
            return raw_json, -1, 0.0, 0, 0

        result_text = data.get("result", "")
        cost_usd = float(data.get("total_cost_usd", 0.0) or 0.0)
        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)

        # Also count cache tokens as input for cost tracking completeness
        input_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
        input_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)

        is_error = data.get("is_error", False)
        exit_hint = 1 if is_error else 0

        return result_text, exit_hint, cost_usd, input_tokens, output_tokens

    @staticmethod
    def _estimate_cost_from_text(prompt: str, output: str, model: str) -> tuple[float, int, int]:
        """Rough cost estimate based on character count for backends without usage data.

        Assumes ~4 chars per token. Returns (cost_usd, est_input_tokens, est_output_tokens).
        Prices are approximate and may be stale.
        """
        est_input = max(len(prompt) // 4, 1)
        est_output = max(len(output) // 4, 1)

        # Conservative price per 1M tokens (input/output)
        # These are rough mid-2025 prices; marked as "estimated" in results
        price_table: dict[str, tuple[float, float]] = {
            # (input_per_1M, output_per_1M)
            "gpt-5.4": (2.50, 10.00),
            "gpt-4.1": (2.00, 8.00),
            "o4-mini": (1.10, 4.40),
        }
        # Default for unknown models (Doubao, DeepSeek, etc.)
        default_price = (1.00, 4.00)

        in_price, out_price = default_price
        for prefix, prices in price_table.items():
            if prefix in model.lower():
                in_price, out_price = prices
                break

        cost = (est_input * in_price + est_output * out_price) / 1_000_000
        return cost, est_input, est_output

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

    # -----------------------------------------------------------------------
    # Visible Terminal — sidecar-protocol based Terminal.app dispatch
    # -----------------------------------------------------------------------
    #
    # Each visible run creates a temp "run directory" with a strict sidecar
    # protocol.  The orchestrator NEVER trusts terminal text as its only
    # source of truth — it relies on sidecar files + artifact detection.
    #
    # Run directory contract:
    #   prompt.txt     — exact prompt text (written by Python before launch)
    #   run.sh         — wrapper script executed in Terminal.app
    #   output.txt     — captured terminal output (tee or script -q)
    #   exit_code.txt  — process exit code (written by cleanup trap)
    #   done.txt       — marker: wrapper has fully completed
    #   pid.txt        — wrapper shell PID for stop/cancel
    #
    # NOTE: In visible interactive mode the prompt is passed as a CLI
    # argument (read from prompt.txt into a shell variable).  This means
    # prompt content is visible via `ps` / Activity Monitor.  This is an
    # accepted limitation of the visible-terminal design.
    # -----------------------------------------------------------------------

    # ANSI escape stripper (reuses pattern from gui.py)
    import re as _re
    _ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return MultiAgentDispatcher._ANSI_RE.sub("", text)

    def _open_terminal(
        self,
        title: str,
        shell_body: str,
        run_dir: str,
        cwd: str | None = None,
        timeout: int = 600,
        cancel_event=None,
    ) -> tuple[str, int]:
        """Open a wrapper script in Terminal.app using the sidecar protocol.

        Args:
            title:      Human-readable title shown in the terminal header.
            shell_body: The CLI command(s) to run (inserted into run.sh).
            run_dir:    Path to the sidecar run directory (already created
                        by the caller with prompt.txt written).
            cwd:        Working directory for the shell script.
            timeout:    Max seconds to wait for done.txt.
            cancel_event: threading.Event — set to abort.

        Returns:
            (output_text, exit_code) read from the sidecar files.
            output_text is ANSI-stripped.  exit_code is 124 on timeout.
        """
        done_path = os.path.join(run_dir, "done.txt")
        exit_code_path = os.path.join(run_dir, "exit_code.txt")
        pid_path = os.path.join(run_dir, "pid.txt")
        output_path = os.path.join(run_dir, "output.txt")
        self._terminal_pid_file = pid_path

        cd_line = f"cd {shlex.quote(cwd)}" if cwd else ""

        # Every run.sh follows the wrapper contract:
        # - set -o pipefail for reliable exit codes through pipes
        # - trap _cleanup to ALWAYS write exit_code.txt + done.txt
        # - TERM=xterm-256color for proper ANSI rendering
        script = f"""#!/bin/zsh
set -o pipefail
export TERM=xterm-256color
RUN_DIR={shlex.quote(run_dir)}
{cd_line}

# Record PID for stop/cancel
echo $$ > "$RUN_DIR/pid.txt"

# Cleanup trap — guarantees sidecar files even on crash/signal
_cleanup() {{
  local rc=$?
  print -r -- "$rc" > "$RUN_DIR/exit_code.txt"
  touch "$RUN_DIR/done.txt"
}}
trap _cleanup EXIT INT TERM

echo ""
echo "  ┌─ {title}"
echo "  └─ $(pwd)"
echo ""

# --- Tool command (inserted by caller) ---
{shell_body}
"""
        script_path = os.path.join(run_dir, "run.sh")
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)

        # Open in Terminal.app via AppleScript
        escaped_path = script_path.replace("\\", "\\\\").replace('"', '\\"')
        osa = (
            'tell application "Terminal"\n'
            "  activate\n"
            f'  do script "{escaped_path}"\n'
            "end tell"
        )
        subprocess.run(["osascript", "-e", osa], capture_output=True)
        print(f"  [Terminal.app] Opened: {title}", flush=True)

        # Poll for done.txt
        start_t = time.time()
        while time.time() - start_t < timeout:
            if cancel_event and cancel_event.is_set():
                self._kill_terminal_process(pid_path)
                break
            if os.path.exists(done_path):
                time.sleep(1)  # let script/tee flush final bytes
                break
            time.sleep(3)

        self._terminal_pid_file = None

        # --- Read sidecar results ---
        exit_code = 124  # default: timeout
        if os.path.exists(exit_code_path):
            try:
                exit_code = int(open(exit_code_path).read().strip())
            except (ValueError, OSError):
                exit_code = 1
        elif os.path.exists(done_path):
            # done.txt exists but exit_code.txt missing → wrapper failure
            exit_code = 1

        output = ""
        if os.path.exists(output_path):
            try:
                raw = open(output_path, encoding="utf-8", errors="replace").read()
                output = self._strip_ansi(raw)
            except Exception:
                pass

        return output, exit_code

    def _kill_terminal_process(self, pid_path: str):
        """Kill the process group running in a Terminal.app window."""
        # Retry briefly in case the script hasn't written pid.txt yet
        for _ in range(5):
            if os.path.exists(pid_path):
                break
            time.sleep(0.5)
        if not os.path.exists(pid_path):
            return
        try:
            pid = int(open(pid_path).read().strip())
            os.killpg(os.getpgid(pid), 9)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass

    # -----------------------------------------------------------------------
    # Internal — Claude Code CLI
    # -----------------------------------------------------------------------

    def _run_claude(self, prompt: str, allowed_tools: str, model: str,
                    effort: str = "high") -> tuple[str, int, float, int, int]:
        """Run Claude Code CLI. Returns (output, exit_code, cost_usd, in_tokens, out_tokens)."""
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--model", model,
            "--effort", effort,
            "--allowedTools", allowed_tools,
        ]
        timeout = 900 if effort == "max" else 600
        print(f"  $ {' '.join(cmd)} <<< (prompt {len(prompt)} chars, timeout {timeout}s)", flush=True)

        # --- Visible Terminal mode: claude -p with tee for real-time display ---
        # NOTE: `script -q` + interactive `claude "prompt"` does NOT work —
        # script's PTY layer swallows the positional prompt argument.
        # Instead we use `claude -p` (piped mode) with `tee` so the user
        # sees Claude's text output streaming in real-time, and we capture
        # the JSON result for cost tracking + artifact detection.
        if self.visible_terminal:
            if len(prompt.encode("utf-8")) > 200_000:
                print("  ⚠ Prompt too large for visible terminal mode, falling back to piped mode")
            else:
                run_dir = tempfile.mkdtemp(prefix="ra-claude-")
                prompt_path = os.path.join(run_dir, "prompt.txt")
                output_path = os.path.join(run_dir, "output.txt")
                with open(prompt_path, "w", encoding="utf-8") as f:
                    f.write(prompt)

                shell_body = (
                    f"cat {shlex.quote(prompt_path)}"
                    f" | claude -p"
                    f" --output-format json"
                    f" --model {shlex.quote(model)}"
                    f" --effort {shlex.quote(effort)}"
                    f" --allowedTools {shlex.quote(allowed_tools)}"
                    f" 2>&1 | tee {shlex.quote(output_path)}"
                )
                output, exit_code = self._open_terminal(
                    title=f"Claude Code — {model} ({effort})",
                    shell_body=shell_body,
                    run_dir=run_dir,
                    cwd=str(self.project_dir),
                    timeout=timeout,
                )
                # Parse JSON from captured output for real cost data
                if output.strip():
                    result_text, exit_hint, cost_usd, in_tok, out_tok = self._parse_claude_json(output)
                    if exit_hint != -1:
                        return result_text, exit_code, cost_usd, in_tok, out_tok
                cost_usd, in_tok, out_tok = self._estimate_cost_from_text(
                    prompt, output, model)
                return output, exit_code, cost_usd, in_tok, out_tok

        # --- Background mode (piped subprocess) ---
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(self.project_dir),
            start_new_session=True,
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "agent-dispatch"},
        )
        self._active_proc = proc
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            stdout, stderr = proc.communicate()
            raise
        finally:
            self._active_proc = None

        # Parse JSON output for result text and usage data
        raw = stdout
        if proc.returncode != 0 and not raw.strip():
            return stderr, proc.returncode, 0.0, 0, 0

        result_text, exit_hint, cost_usd, in_tok, out_tok = self._parse_claude_json(raw)

        # If JSON parsing failed (exit_hint == -1), fall back to raw stdout
        if exit_hint == -1:
            return raw, proc.returncode, 0.0, 0, 0

        exit_code = proc.returncode
        return result_text, exit_code, cost_usd, in_tok, out_tok

    # -----------------------------------------------------------------------
    # Internal — OpenCode CLI
    # -----------------------------------------------------------------------

    def _run_opencode(self, prompt: str, model: str,
                      effort: str = "high",
                      expected_files: list[str] | None = None,
                      progress_fn=None,
                      cancel_event=None,
                      ) -> tuple[str, int]:
        """Run opencode run with a prompt. Returns (output, exit_code).

        Key design: opencode starts a local server and may NOT exit after task
        completion. We use Popen + file-based completion detection + heartbeat.
        """
        cmd = [
            OPENCODE_BIN, "run",
            "--dir", str(self.project_dir),
        ]
        if model:
            cmd.extend(["-m", model])
        if effort and effort != "none":
            cmd.extend(["--variant", effort])
        cmd.append(prompt)

        # OpenCode agents are slower (subagents + web fetching) — need more time
        # But if no expected_files (e.g. critic), use shorter timeout
        if expected_files:
            timeout = 1800 if effort == "max" else 1200
        else:
            timeout = 600  # Critic / no-file tasks: 10 min max
        _log = progress_fn or (lambda msg: None)
        # Show the CLI command (truncate prompt to keep it readable)
        cmd_display = cmd[:-1] + [f"'({len(prompt)} chars)'"]
        print(f"  $ {' '.join(cmd_display)}  (timeout {timeout}s)", flush=True)

        # --- Visible Terminal mode: opencode in Terminal.app ---
        if self.visible_terminal:
            run_dir = tempfile.mkdtemp(prefix="ra-opencode-")
            prompt_path = os.path.join(run_dir, "prompt.txt")
            output_path = os.path.join(run_dir, "output.txt")
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(prompt)

            # Build opencode command; prompt read from file into variable
            oc_args = f"run --dir {shlex.quote(str(self.project_dir))}"
            if model:
                oc_args += f" -m {shlex.quote(model)}"
            if effort and effort != "none":
                oc_args += f" --variant {shlex.quote(effort)}"

            shell_body = (
                f'_P="$(cat {shlex.quote(prompt_path)})"\n'
                f"{shlex.quote(OPENCODE_BIN)} {oc_args}"
                f' "$_P" 2>&1 | tee {shlex.quote(output_path)}'
            )
            output, exit_code = self._open_terminal(
                title=f"OpenCode — {model} ({effort})",
                shell_body=shell_body,
                run_dir=run_dir,
                cwd=str(self.project_dir),
                timeout=timeout,
                cancel_event=cancel_event,
            )
            if not output.strip() and expected_files:
                found = [(self.project_dir / f).exists() for f in expected_files]
                if all(found):
                    output = f"Files produced: {expected_files}"
            return output, exit_code

        # --- Background mode (piped subprocess with poll loop) ---
        # Monitor opencode's tool-output directory for activity
        tool_dir = Path.home() / ".local" / "share" / "opencode" / "tool-output"
        tool_baseline = set(tool_dir.glob("*")) if tool_dir.exists() else set()

        # Start in new session so we can kill the entire process tree
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        _log(f"  Process started (PID {proc.pid}, timeout {timeout}s)")

        # Background thread to drain stdout
        output_lines: list[str] = []
        import threading
        def _drain():
            try:
                for line in proc.stdout:
                    output_lines.append(line)
            except Exception:
                pass
        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()

        # Poll loop: check for file completion + report heartbeat
        start_t = time.time()
        files_found = False
        last_heartbeat = 0

        while time.time() - start_t < timeout:
            elapsed = int(time.time() - start_t)

            # Cancelled by user?
            if cancel_event and cancel_event.is_set():
                _log(f"  Cancelled by user ({elapsed}s)")
                break

            # Process exited?
            if proc.poll() is not None:
                _log(f"  Process exited (code {proc.returncode}, {elapsed}s)")
                break

            # Expected files appeared?
            if expected_files:
                found = [(self.project_dir / f).exists() for f in expected_files]
                if all(found):
                    files_found = True
                    _log(f"  Output files detected! ({elapsed}s)")
                    time.sleep(3)  # Let opencode finish cleanup
                    break
            else:
                # No expected files (critic mode): check if stdout has verdict
                current_output = "".join(output_lines)
                if "verdict:" in current_output.lower() and "```" in current_output:
                    files_found = True  # Use this flag to signal "output ready"
                    _log(f"  Review verdict received ({elapsed}s)")
                    time.sleep(2)
                    break

            # Heartbeat every 15 seconds with activity detection
            if elapsed - last_heartbeat >= 15:
                last_heartbeat = elapsed
                # Detect opencode tool activity
                activity = ""
                if tool_dir.exists():
                    current_tools = set(tool_dir.glob("*"))
                    new_tools = current_tools - tool_baseline
                    if new_tools:
                        latest = max(new_tools, key=lambda p: p.stat().st_mtime)
                        size_kb = latest.stat().st_size // 1024
                        activity = f" | tool activity: {latest.name} ({size_kb}KB)"
                        tool_baseline = current_tools  # Update baseline

                _log(f"  Working... ({elapsed}s){activity}")

            time.sleep(5)

        # Kill process tree
        exit_code = proc.poll()
        if exit_code is None:
            elapsed = int(time.time() - start_t)
            if files_found:
                _log(f"  Stopping opencode (files complete, {elapsed}s)")
            else:
                _log(f"  Timeout ({elapsed}s), killing process")
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except Exception:
                    pass
            exit_code = 0 if files_found else 124

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
        from .integrations.codex import codex_review, build_review_prompt, parse_codex_review
        from .agents.critic import STAGE_REVIEW_CRITERIA

        context_parts = []
        for f in task.context_files:
            p = self.project_dir / f
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                context_parts.append(self._yaml_to_readable(p.name, raw))

        criteria = STAGE_REVIEW_CRITERIA.get(task.stage.value, "Review for rigor.")
        codex_model = self.config.get("agents", {}).get("critic", {}).get("model", "gpt-5.4")
        codex_effort = self.config.get("agents", {}).get("critic", {}).get("effort", "xhigh")

        start = time.time()

        # --- Visible Terminal mode: codex in Terminal.app ---
        if self.visible_terminal:
            run_dir = tempfile.mkdtemp(prefix="ra-codex-")
            review_prompt = build_review_prompt(
                task.stage.value, "\n\n".join(context_parts), criteria, task.instruction)
            prompt_path = os.path.join(run_dir, "prompt.txt")
            output_path = os.path.join(run_dir, "output.txt")
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(review_prompt)

            # Codex has no TUI — use exec with tee for real-time visibility
            codex_cmd = "codex exec"
            if codex_model:
                codex_cmd += f" --model {shlex.quote(codex_model)}"
            if codex_effort and codex_effort != "none":
                codex_cmd += f" -c 'reasoning_effort=\"{codex_effort}\"'"

            shell_body = (
                f'_P="$(cat {shlex.quote(prompt_path)})"\n'
                f'{codex_cmd} "$_P" 2>&1 | tee {shlex.quote(output_path)}'
            )
            raw_output, exit_code = self._open_terminal(
                title=f"Codex Critic — {codex_model} ({codex_effort})",
                shell_body=shell_body,
                run_dir=run_dir,
                cwd=str(self.project_dir),
                timeout=900,
            )
            result = parse_codex_review(raw_output)
            result.exit_code = exit_code
        else:
            result = codex_review(
                stage=task.stage.value,
                artifact_content="\n\n".join(context_parts),
                review_criteria=criteria,
                project_context=task.instruction,
                model=codex_model,
                effort=codex_effort,
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

        # Codex CLI does not expose token usage — estimate from text
        codex_model = self.config.get("agents", {}).get("critic", {}).get("model", "gpt-5.4")
        prompt_text = task.instruction + "\n".join(context_parts)
        est_cost, est_in, est_out = self._estimate_cost_from_text(
            prompt_text, result.raw_output, codex_model)

        return AgentResult(
            task_id=task.task_id, role=AgentRole.CRITIC,
            success=result.verdict == "PASS",
            output_text=result.raw_output,
            output_files=[str(review_path)],
            duration_seconds=duration,
            cost_usd=est_cost, input_tokens=est_in, output_tokens=est_out,
            cost_source="estimated",
        )

    # -----------------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------------

    def _detect_output_files(self, task: TaskCard, output: str,
                             dispatch_start: float | None = None) -> list[str]:
        """Detect output files written by the agent during this dispatch.

        Args:
            dispatch_start: time.time() when the dispatch began. When set,
                files with mtime < dispatch_start are rejected as stale.
                Callers MUST pass this; None disables freshness checks
                (only for backward-compat with codex path).

        Two-pass strategy:
        1. Check expected files (required_outputs) — only accept if fresh.
        2. Fallback: glob the stage dir for NEW files only, filtered by:
           a. mtime >= dispatch_start (reject stale pre-existing files)
           b. stem matches a stage-required ArtifactType (reject wrong types)
        """
        found = set()
        for expected in task.required_outputs:
            p = self.project_dir / expected
            if p.exists():
                if dispatch_start is not None:
                    try:
                        if p.stat().st_mtime < dispatch_start:
                            continue
                    except OSError:
                        continue
                found.add(expected)

        if not found:
            pid = task.metadata.get("project_id", "")
            if pid:
                art_dir = self.project_dir / "projects" / pid / "artifacts" / task.stage.value
                if art_dir.exists():
                    # Only accept artifact types required for this stage
                    stage_types = STAGE_REQUIRED_ARTIFACTS.get(task.stage, [])
                    known_types = {at.value for at in stage_types} if stage_types else {at.value for at in ArtifactType}
                    for f in art_dir.glob("*.yaml"):
                        if f.name.startswith("review_"):
                            continue
                        if dispatch_start is not None:
                            try:
                                if f.stat().st_mtime < dispatch_start:
                                    continue
                            except OSError:
                                continue
                        # Reject files whose name doesn't match any ArtifactType
                        stem = f.stem
                        if not any(at in stem for at in known_types):
                            continue
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
