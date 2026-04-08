"""Shared code materialization and experiment execution.

Extracted from gui.py to be usable by both the GUI (web) path and the
CLI (multi_agent.py) path. The key invariant is that IMPLEMENTATION stage
must materialize code to disk, and EXPERIMENTATION stage must run the
smoke test — regardless of which entry point is used.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Callable, Optional

import yaml

from .models import AgentRole, ArtifactType, ProjectState, Stage
from .state import StateManager
from .artifacts import register_artifact_file, safe_parse_yaml


LogFn = Callable[[str], None]


def _noop_log(msg: str) -> None:
    pass


# Safe command prefixes — only these binaries are allowed in agent-authored commands
_SAFE_CMD_PREFIXES = ("python", "python3", "pytest", "pip", "pip3")


def _validate_command(cmd: str) -> tuple[bool, str]:
    """Validate an agent-authored shell command against the allowlist.

    Returns (is_safe, reason).
    Only simple commands starting with allowed prefixes are permitted.
    Compound commands (&&, ||, ;, |) and shell features ($(), backticks) are rejected.
    """
    stripped = cmd.strip()
    if not stripped:
        return False, "empty command"

    # Reject shell operators
    for op in ("&&", "||", ";", "|", "`", "$(", ">", "<"):
        if op in stripped:
            return False, f"shell operator '{op}' not allowed"

    try:
        parts = shlex.split(stripped)
    except ValueError as e:
        return False, f"cannot parse command: {e}"

    if not parts:
        return False, "empty command after parsing"

    binary = parts[0].split("/")[-1]  # Handle full paths like /usr/bin/python3
    if binary not in _SAFE_CMD_PREFIXES:
        return False, f"binary '{binary}' not in allowlist {_SAFE_CMD_PREFIXES}"

    return True, "ok"


def materialize_code(
    state: ProjectState,
    sm: StateManager,
    project_id: str,
    base_dir: Path,
    log_fn: Optional[LogFn] = None,
) -> list[str]:
    """Extract code from YAML code artifacts into actual files on disk.

    Reads CODE artifacts from the IMPLEMENTATION stage, parses the `files`
    list, and writes each entry to `projects/<id>/<path>`.

    Returns list of materialized relative file paths.
    """
    log = log_fn or _noop_log
    materialized: list[str] = []

    for art in state.stage_artifacts(Stage.IMPLEMENTATION):
        if art.artifact_type != ArtifactType.CODE:
            continue
        try:
            content = sm.read_artifact_file(project_id, art)
            data = safe_parse_yaml(content)
            for file_entry in (data or {}).get("files", []):
                rel_path = file_entry.get("path", "")
                code_content = file_entry.get("content", "")
                if not rel_path or not code_content:
                    continue
                out = base_dir / "projects" / project_id / rel_path
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(code_content, encoding="utf-8")
                materialized.append(rel_path)
                log(f"  Materialized: {rel_path}")
        except Exception as e:
            log(f"  Materialize error: {e}")

    return materialized


def execute_experiment(
    state: ProjectState,
    sm: StateManager,
    project_id: str,
    base_dir: Path,
    log_fn: Optional[LogFn] = None,
) -> tuple[Optional[str], int]:
    """Run the smoke test from run_manifest and capture output.

    Reads the latest RUN_MANIFEST artifact, runs environment_setup commands,
    then runs the smoke_test_command.

    Returns (stdout_stderr, exit_code). exit_code is -1 if no manifest found,
    -2 for timeout, -3 for other errors.
    """
    log = log_fn or _noop_log
    proj_dir = base_dir / "projects" / project_id

    # Find latest run_manifest
    manifest_art = None
    for art in reversed(state.artifacts):
        if art.artifact_type == ArtifactType.RUN_MANIFEST:
            manifest_art = art
            break
    if not manifest_art:
        log("  No run_manifest found. Skipping execution.")
        return None, -1

    try:
        raw = sm.read_artifact_file(project_id, manifest_art)
        manifest = safe_parse_yaml(raw) or {}
    except Exception as e:
        log(f"  Failed to read run_manifest: {e}")
        return None, -1

    # Run environment setup commands first (validated against allowlist)
    for setup_cmd in manifest.get("environment_setup", []):
        safe, reason = _validate_command(setup_cmd)
        if not safe:
            log(f"  Setup SKIPPED (unsafe: {reason}): {setup_cmd}")
            continue
        log(f"  Setup: {setup_cmd}")
        try:
            subprocess.run(
                shlex.split(setup_cmd),
                shell=False,
                cwd=str(proj_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as e:
            log(f"  Setup warning: {e}")

    # Run smoke test (validated against allowlist)
    smoke_cmd = manifest.get("smoke_test_command", "")
    if not smoke_cmd:
        log("  No smoke_test_command in manifest.")
        return None, -1

    safe, reason = _validate_command(smoke_cmd)
    if not safe:
        log(f"  Smoke test BLOCKED (unsafe: {reason}): {smoke_cmd}")
        return f"Command blocked: {reason}", -4

    log(f"  Running smoke test: {smoke_cmd}")
    try:
        result = subprocess.run(
            shlex.split(smoke_cmd),
            shell=False,
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        icon = "OK" if result.returncode == 0 else "FAIL"
        log(f"  Smoke test {icon} (exit {result.returncode})")
        if result.returncode != 0:
            log(f"  stderr: {result.stderr[:500]}")
        return output, result.returncode
    except subprocess.TimeoutExpired:
        log("  Smoke test timed out (600s)")
        return "TIMEOUT", -2
    except Exception as e:
        log(f"  Execution error: {e}")
        return str(e), -3


# ---------------------------------------------------------------------------
# Orchestrator-side: run and record VERIFIED results
# ---------------------------------------------------------------------------

def run_and_record_tests(
    state: ProjectState,
    sm: StateManager,
    project_id: str,
    base_dir: Path,
    log_fn: Optional[LogFn] = None,
) -> dict:
    """Orchestrator: materialize code, run tests, write ACTUAL test_result artifact.

    Steps:
    1. materialize_code() — extract files from YAML to disk
    2. Find test files and run pytest
    3. Run smoke_test_command from run_manifest
    4. Write verified test_result artifact (overrides agent draft)

    Returns {"passed": bool, "test_output": str, "artifact_path": str, "materialized": list}
    """
    log = log_fn or _noop_log
    proj_dir = base_dir / "projects" / project_id
    result: dict = {"passed": False, "test_output": "", "artifact_path": "", "materialized": []}

    # Step 1: Materialize code
    materialized = materialize_code(state, sm, project_id, base_dir, log_fn)
    result["materialized"] = materialized
    if not materialized:
        log("  [Orchestrator] No code materialized — cannot run tests")
        result["test_output"] = "No code files materialized from code artifact"
        _write_test_result(state, sm, project_id, base_dir, False, result["test_output"], log)
        return result

    # Step 2: Find and run test files
    test_files = []
    for f in proj_dir.rglob("*.py"):
        if f.name.startswith("test_") or f.name.endswith("_test.py"):
            test_files.append(f)

    test_outputs: list[str] = []
    all_passed = True

    if test_files:
        for tf in test_files[:5]:  # Cap at 5 test files
            log(f"  [Orchestrator] Running pytest: {tf.relative_to(proj_dir)}")
            try:
                r = subprocess.run(
                    ["python", "-m", "pytest", str(tf), "--tb=short", "-q"],
                    cwd=str(proj_dir), capture_output=True, text=True, timeout=120,
                )
                test_outputs.append(f"=== {tf.name} (exit {r.returncode}) ===\n{r.stdout}\n{r.stderr}")
                if r.returncode != 0:
                    all_passed = False
            except subprocess.TimeoutExpired:
                test_outputs.append(f"=== {tf.name} TIMEOUT ===")
                all_passed = False
            except Exception as e:
                test_outputs.append(f"=== {tf.name} ERROR: {e} ===")
                all_passed = False

    # Step 3: Run smoke test from run_manifest
    smoke_output, smoke_exit = execute_experiment(state, sm, project_id, base_dir, log_fn)
    if smoke_output:
        test_outputs.append(f"=== smoke test (exit {smoke_exit}) ===\n{smoke_output}")
        if smoke_exit != 0:
            all_passed = False

    combined_output = "\n\n".join(test_outputs) if test_outputs else "No tests found or run"
    result["passed"] = all_passed
    result["test_output"] = combined_output

    # Step 4: Write verified test_result artifact
    artifact_path = _write_test_result(state, sm, project_id, base_dir, all_passed, combined_output, log)
    result["artifact_path"] = artifact_path

    icon = "PASS" if all_passed else "FAIL"
    log(f"  [Orchestrator] Tests {icon} — verified test_result written")
    return result


def _write_test_result(
    state: ProjectState,
    sm: StateManager,
    project_id: str,
    base_dir: Path,
    passed: bool,
    test_output: str,
    log_fn: Optional[LogFn] = None,
) -> str:
    """Write a verified test_result YAML artifact to disk and register in state."""
    log = log_fn or _noop_log
    stage_dir = base_dir / "projects" / project_id / "artifacts" / "implementation"
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Build YAML content matching test_result schema
    content = yaml.dump({
        "test_summary": f"Orchestrator-verified test execution ({'all passed' if passed else 'some failed'})",
        "overall_status": "all_passed" if passed else "some_failed",
        "passed_tests": [],
        "failed_tests": [],
        "test_output": test_output[:5000],  # Truncate to avoid huge files
        "verified_by": "orchestrator",
    }, default_flow_style=False, allow_unicode=True)

    # Write to a temp file, register will rename to canonical version
    existing = [a for a in state.artifacts if a.artifact_type == ArtifactType.TEST_RESULT]
    next_ver = max((a.version for a in existing), default=0) + 1
    filename = f"test_result_v{next_ver}.yaml"
    filepath = stage_dir / filename
    filepath.write_text(content, encoding="utf-8")

    register_artifact_file(
        state, ArtifactType.TEST_RESULT, Stage.IMPLEMENTATION,
        AgentRole.ORCHESTRATOR, filepath,
        base_dir / "projects" / project_id,
        metadata={"verified_by": "orchestrator", "iteration": state.current_iteration()},
    )
    sm.save_project(state)
    log(f"  [Orchestrator] Wrote verified {filename}")
    return str(filepath)


def run_and_record_experiment(
    state: ProjectState,
    sm: StateManager,
    project_id: str,
    base_dir: Path,
    log_fn: Optional[LogFn] = None,
) -> dict:
    """Orchestrator: execute experiment, parse real metrics, write ACTUAL metrics artifact.

    Steps:
    1. execute_experiment() — run smoke test
    2. Parse stdout for metric patterns
    3. Write verified metrics artifact (overrides agent draft)
    4. Save execution log

    Returns {"success": bool, "metrics": dict, "log_path": str, "raw_output": str}
    """
    log = log_fn or _noop_log
    proj_dir = base_dir / "projects" / project_id
    result: dict = {"success": False, "metrics": {}, "log_path": "", "raw_output": ""}

    # Step 1: Execute experiment
    raw_output, exec_exit = execute_experiment(state, sm, project_id, base_dir, log_fn)
    if not raw_output:
        log("  [Orchestrator] No experiment output — cannot record metrics")
        _write_metrics(state, sm, project_id, base_dir, {}, "No experiment output", log, exit_code=-1)
        return result
    result["raw_output"] = raw_output
    result["exit_code"] = exec_exit

    # Step 2: Save execution log
    log_dir = proj_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "experiment_execution.txt"
    log_path.write_text(raw_output, encoding="utf-8")
    result["log_path"] = str(log_path)

    # Step 3: Parse metrics from multiple sources
    import json as _json
    import re
    metrics: dict[str, float] = {}

    # Source A: Read expected_outputs files from run_manifest (metrics.json, etc.)
    manifest_art = None
    for art in reversed(state.artifacts):
        if art.artifact_type == ArtifactType.RUN_MANIFEST:
            manifest_art = art
            break
    if manifest_art:
        try:
            manifest_raw = sm.read_artifact_file(project_id, manifest_art)
            manifest_data = safe_parse_yaml(manifest_raw) or {}
            for expected_file in manifest_data.get("expected_outputs", []):
                fpath = proj_dir / expected_file
                if fpath.exists() and fpath.stat().st_size < 1_000_000:
                    try:
                        file_text = fpath.read_text(encoding="utf-8")
                        # Try JSON
                        if fpath.suffix == ".json":
                            parsed = _json.loads(file_text)
                            if isinstance(parsed, dict):
                                for k, v in parsed.items():
                                    if isinstance(v, (int, float)):
                                        metrics[k.lower()] = float(v)
                        # Try YAML
                        elif fpath.suffix in (".yaml", ".yml"):
                            parsed = yaml.safe_load(file_text)
                            if isinstance(parsed, dict):
                                for k, v in parsed.items():
                                    if isinstance(v, (int, float)):
                                        metrics[k.lower()] = float(v)
                        log(f"  [Orchestrator] Parsed {len(metrics)} metrics from {expected_file}")
                    except Exception:
                        pass
        except Exception:
            pass

    # Source B: Parse stdout/stderr for metric patterns (fallback)
    for m in re.finditer(r'(?:^|\s)([\w_]+)\s*[=:]\s*([0-9]+\.?[0-9]*(?:e[+-]?[0-9]+)?)', raw_output):
        name, val = m.group(1).lower(), m.group(2)
        if name not in metrics:  # Don't override file-sourced values
            try:
                metrics[name] = float(val)
            except ValueError:
                pass
    for m in re.finditer(r'"([\w_]+)"\s*:\s*([0-9]+\.?[0-9]*)', raw_output):
        name, val = m.group(1).lower(), m.group(2)
        if name not in metrics:
            try:
                metrics[name] = float(val)
            except ValueError:
                pass

    result["metrics"] = metrics
    result["success"] = exec_exit == 0

    # Step 4: Write verified metrics artifact
    _write_metrics(state, sm, project_id, base_dir, metrics, raw_output, log, exit_code=exec_exit)

    log(f"  [Orchestrator] Metrics recorded: {len(metrics)} values parsed")
    return result


def _write_metrics(
    state: ProjectState,
    sm: StateManager,
    project_id: str,
    base_dir: Path,
    metrics: dict,
    raw_output: str,
    log_fn: Optional[LogFn] = None,
    exit_code: int = -1,
) -> str:
    """Write a verified metrics YAML artifact to disk and register in state."""
    log = log_fn or _noop_log
    stage_dir = base_dir / "projects" / project_id / "artifacts" / "experimentation"
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Build metrics_summary from parsed metrics, referencing agent draft targets if available
    metrics_summary = []
    for name, value in metrics.items():
        metrics_summary.append({"name": name, "current": value, "target": None})

    content = yaml.dump({
        "experiment_name": f"orchestrator_verified_{project_id}",
        "metrics_summary": metrics_summary or [{"name": "no_metrics_parsed", "current": 0, "target": None}],
        "exit_code": exit_code,
        "execution_success": exit_code == 0,
        "raw_output_excerpt": raw_output[:2000],
        "verified_by": "orchestrator",
    }, default_flow_style=False, allow_unicode=True)

    existing = [a for a in state.artifacts if a.artifact_type == ArtifactType.METRICS]
    next_ver = max((a.version for a in existing), default=0) + 1
    filename = f"metrics_v{next_ver}.yaml"
    filepath = stage_dir / filename
    filepath.write_text(content, encoding="utf-8")

    register_artifact_file(
        state, ArtifactType.METRICS, Stage.EXPERIMENTATION,
        AgentRole.ORCHESTRATOR, filepath,
        base_dir / "projects" / project_id,
        metadata={"verified_by": "orchestrator", "iteration": state.current_iteration()},
    )
    sm.save_project(state)
    log(f"  [Orchestrator] Wrote verified {filename}")
    return str(filepath)
