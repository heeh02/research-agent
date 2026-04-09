"""Terminal Session Manager — persistent Terminal.app tabs with worker loops.

Each (stage, role) pair gets ONE Terminal tab with a long-lived worker script.
The orchestrator communicates via sidecar files — AppleScript is only used once
per session to launch the worker.

Worker directory contract:
  worker.sh         -- long-lived loop script (launched once via AppleScript)
  command_N.sh      -- command to execute for iteration N (written by Python)
  prompt_N.txt      -- prompt text for iteration N
  output_N.txt      -- captured output from iteration N
  output_N.json     -- JSON output (claude -p) for iteration N
  exit_code_N.txt   -- exit code for iteration N
  done_N.txt        -- completion marker for iteration N
  session_id.txt    -- last CLI session ID (for resume)
  pid.txt           -- worker shell PID
  shutdown.txt      -- shutdown signal
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import AgentRole, Stage

# ANSI escape stripper
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Worker script template — runs in Terminal.app as a long-lived loop
_WORKER_SCRIPT = """\
#!/bin/zsh
set -o pipefail
export TERM=xterm-256color
WDIR="$1"
TITLE="$2"

echo $$ > "$WDIR/pid.txt"
trap 'touch "$WDIR/shutdown.txt"; exit 0' INT TERM

echo ""
echo "  ┌─ Worker: $TITLE"
echo "  └─ $(pwd)"
echo ""

N=0
while true; do
  N=$((N + 1))
  CMDFILE="$WDIR/command_${N}.sh"
  DONEFILE="$WDIR/done_${N}.txt"
  EXITFILE="$WDIR/exit_code_${N}.txt"

  # Wait for command file (poll every 0.5s)
  while [ ! -f "$CMDFILE" ]; do
    [ -f "$WDIR/shutdown.txt" ] && { echo "  [Worker] Shutdown signal received."; exit 0; }
    sleep 0.5
  done

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Iteration $N — $(date '+%H:%M:%S')"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  rm -f "$DONEFILE" "$EXITFILE"

  # Export iteration info — command scripts use these to signal
  # completion BEFORE any post-task interactive steps (e.g. TUI).
  # This prevents interactive commands from blocking the Python dispatcher.
  export RA_ITER=$N
  export RA_WDIR="$WDIR"
  export RA_DONE="$DONEFILE"
  export RA_EXIT="$EXITFILE"

  # Source the command script (runs in this shell's context)
  source "$CMDFILE"

  # Fallback: write markers if command script didn't
  [ ! -f "$EXITFILE" ] && echo "$?" > "$EXITFILE"
  [ ! -f "$DONEFILE" ] && touch "$DONEFILE"

  echo ""
  echo "  [Worker] Iteration $N done"
  echo ""
done
"""


# Session key: (project_id, stage, role) — isolates sessions across projects
SessionKey = tuple[str, str, str]


@dataclass
class TerminalSession:
    """A persistent Terminal.app tab bound to a (project_id, stage, role) triple."""
    project_id: str
    stage: Stage
    role: AgentRole
    worker_dir: Path
    cwd: str = ""            # Working directory (preserved across restarts)
    iteration: int = 0
    session_id: str = ""     # CLI session ID for --resume
    _pid: int = 0

    @property
    def key(self) -> SessionKey:
        return (self.project_id, self.stage.value, self.role.value)


class TerminalSessionManager:
    """Manages persistent Terminal.app worker tabs.

    Each (project_id, stage, role) gets one tab. The worker stays alive
    between dispatches within the same stage. Sessions are fully isolated
    across projects.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        # Lazy init — don't mkdir here so read-only commands (status) don't crash
        self._sessions: dict[SessionKey, TerminalSession] = {}

    def _ensure_base_dir(self):
        """Create base directory on first actual use."""
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_or_create(
        self, stage: Stage, role: AgentRole, title: str,
        cwd: str | None = None, project_id: str = "",
    ) -> TerminalSession:
        """Get existing worker or launch a new one in Terminal.app."""
        self._ensure_base_dir()
        key: SessionKey = (project_id, stage.value, role.value)
        session = self._sessions.get(key)

        if session and self.is_alive(session):
            return session

        # Clean up dead session if any
        if session:
            self._cleanup_session(session)

        # Create new worker directory
        worker_dir = Path(tempfile.mkdtemp(
            prefix=f"ra-{stage.value}-{role.value}-",
            dir=str(self.base_dir),
        ))

        session = TerminalSession(
            project_id=project_id, stage=stage, role=role,
            worker_dir=worker_dir, cwd=cwd or "",
        )
        self._sessions[key] = session

        # Write worker script
        script_path = worker_dir / "worker.sh"
        script_path.write_text(_WORKER_SCRIPT, encoding="utf-8")
        script_path.chmod(0o755)

        # Launch in Terminal.app via AppleScript (one-time only)
        cd_part = f"cd {shlex.quote(cwd)} && " if cwd else ""
        cmd_str = f"{cd_part}{shlex.quote(str(script_path))} {shlex.quote(str(worker_dir))} {shlex.quote(title)}"
        escaped = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
        osa = (
            'tell application "Terminal"\n'
            "  activate\n"
            f'  do script "{escaped}"\n'
            "end tell"
        )
        subprocess.run(["osascript", "-e", osa], capture_output=True)
        print(f"  [Terminal] Opened: {title}", flush=True)

        # Wait for worker to write pid.txt
        pid_path = worker_dir / "pid.txt"
        for _ in range(20):  # up to 10 seconds
            if pid_path.exists():
                try:
                    session._pid = int(pid_path.read_text().strip())
                    break
                except (ValueError, OSError):
                    pass
            time.sleep(0.5)

        return session

    def send_command(
        self,
        session: TerminalSession,
        command_script: str,
        prompt: str = "",
        timeout: int = 900,
    ) -> tuple[str, int]:
        """Send a command to the worker and wait for completion.

        Args:
            session: The terminal session to send to.
            command_script: Shell script content to execute.
            prompt: Prompt text (written to prompt_N.txt for the command to read).
            timeout: Max seconds to wait for done_N.txt.

        Returns:
            (output_text, exit_code). output_text is ANSI-stripped.
        """
        # Relaunch worker if dead — preserve project_id, cwd, session_id
        if not self.is_alive(session):
            print(f"  [Terminal] Worker died, relaunching...", flush=True)
            old_sid = session.session_id
            old_project_id = session.project_id
            old_cwd = session.cwd
            key = session.key
            self._sessions.pop(key, None)
            session = self.get_or_create(
                session.stage, session.role,
                f"{session.role.value}@{session.stage.value} (restarted)",
                cwd=old_cwd or None,
                project_id=old_project_id,
            )
            session.session_id = old_sid  # preserve session_id for resume

        session.iteration += 1
        n = session.iteration
        wd = session.worker_dir

        # Write prompt file
        if prompt:
            prompt_path = wd / f"prompt_{n}.txt"
            prompt_path.write_text(prompt, encoding="utf-8")

        # Write command script (this triggers the worker)
        cmd_path = wd / f"command_{n}.sh"
        cmd_path.write_text(command_script, encoding="utf-8")

        # Poll for done_N.txt
        done_path = wd / f"done_{n}.txt"
        exit_path = wd / f"exit_code_{n}.txt"
        output_path = wd / f"output_{n}.txt"
        json_path = wd / f"output_{n}.json"

        start_t = time.time()
        while time.time() - start_t < timeout:
            if done_path.exists():
                time.sleep(1)  # let tee/redirect flush
                break
            time.sleep(2)

        # Read exit code
        exit_code = 124  # timeout default
        if exit_path.exists():
            try:
                exit_code = int(exit_path.read_text().strip())
            except (ValueError, OSError):
                exit_code = 1

        # Read output — prefer JSON (claude), fall back to txt
        output = ""
        for path in (json_path, output_path):
            if path.exists():
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    output = _ANSI_RE.sub("", raw)
                    break
                except Exception:
                    pass

        # Read updated session_id if worker wrote one
        sid_path = wd / "session_id.txt"
        if sid_path.exists():
            try:
                sid = sid_path.read_text().strip()
                if sid:
                    session.session_id = sid
            except OSError:
                pass

        return output, exit_code

    def read_session_id(self, session: TerminalSession) -> str:
        """Read the current session_id from the worker dir."""
        sid_path = session.worker_dir / "session_id.txt"
        if sid_path.exists():
            try:
                sid = sid_path.read_text().strip()
                if sid:
                    session.session_id = sid
            except OSError:
                pass
        return session.session_id

    def is_alive(self, session: TerminalSession) -> bool:
        """Check if the worker process is still running."""
        if session._pid <= 0:
            return False
        try:
            os.kill(session._pid, 0)  # signal 0 = check existence
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def close_stage(self, stage: Stage):
        """Kill all workers for a given stage (called on stage advance/rollback).

        Key format is (project_id, stage, role) — match on k[1].
        """
        keys_to_remove = [k for k in self._sessions if k[1] == stage.value]
        for key in keys_to_remove:
            session = self._sessions.pop(key, None)
            if session:
                self._shutdown_session(session)

    def close_all(self):
        """Kill all workers (pipeline shutdown)."""
        for session in list(self._sessions.values()):
            self._shutdown_session(session)
        self._sessions.clear()

    def _shutdown_session(self, session: TerminalSession):
        """Gracefully shut down a worker session."""
        # Write shutdown signal
        shutdown_path = session.worker_dir / "shutdown.txt"
        try:
            shutdown_path.touch()
        except OSError:
            pass

        # Give worker 3s to exit gracefully
        for _ in range(6):
            if not self.is_alive(session):
                return
            time.sleep(0.5)

        # Force kill
        self._kill_session(session)

    def _kill_session(self, session: TerminalSession):
        """Force-kill a worker's process group."""
        if session._pid > 0:
            try:
                os.killpg(os.getpgid(session._pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(session._pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    def _cleanup_session(self, session: TerminalSession):
        """Remove a dead session from tracking."""
        key = session.key
        self._sessions.pop(key, None)
