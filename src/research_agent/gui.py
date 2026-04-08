"""Web GUI — full pipeline control + visualization.

Features:
- Project management (create, switch, list)
- Pipeline control (auto, step, review, approve, stop)
- CLI backend selector (claude/codex/opencode) per agent
- Real-time console output
- Version timeline + detail panel

Launch: python scripts/multi_agent.py gui [--port 8080]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

from .models import (
    ALLOWED_TRANSITIONS, STAGE_ORDER, STAGE_PRIMARY_AGENT, STAGE_REQUIRED_ARTIFACTS,
    AgentRole, ArtifactType, CLIBackend, CostRecord, GateCheck, GateResult, GateStatus,
    LLMProvider, ProjectState, Stage, VersionEventType,
)
from .state import StateManager
from .verdict import (
    evaluate_rollback,
    parse_failure_type, FAILURE_TYPE_CROSS_STAGE,
)
from .gate_eval import evaluate_gate_verdict
from .prechecks import pre_review_checks as shared_pre_review_checks
from .execution import materialize_code as shared_materialize_code
from .execution import execute_experiment as shared_execute_experiment
from .execution import run_and_record_tests, run_and_record_experiment
from .artifacts import create_artifact
from .dispatcher import MultiAgentDispatcher, TaskCard, AgentResult


# ---------------------------------------------------------------------------
# Pipeline Runner — executes pipeline operations in background threads
# ---------------------------------------------------------------------------

# Stage-specific YAML format specs (keeps CLAUDE.md short, only current stage sent)
_STAGE_YAML_FORMATS = {
    Stage.PROBLEM_DEFINITION: (
        "Output format (YAML in ```yaml fences):\n"
        "  domain, problem_statement, motivation, scope,\n"
        "  existing_approaches: [{name, description, key_paper}] (5+),\n"
        "  limitations_of_existing: [] (3+),\n"
        "  proposed_direction, success_criteria,\n"
        "  key_references: [{title, authors, year, venue, relevance}] (7+)"
    ),
    Stage.LITERATURE_REVIEW: (
        "Output format (YAML in ```yaml fences):\n"
        "  research_question, search_scope,\n"
        "  papers: [{title, authors, year, venue, method, key_results, limitations, relevance_score, url}] (10+),\n"
        "  CRITICAL: every paper MUST have a `url` field with the real URL where you found it.\n"
        "  method_taxonomy: {category: [methods]},\n"
        "  identified_gaps: [] (3+), conflicting_findings, trend_analysis,\n"
        "  recommended_baselines: [] (3+)"
    ),
    Stage.HYPOTHESIS_FORMATION: (
        "Output format (YAML in ```yaml fences):\n"
        "  claim, motivation, why_now, novelty_argument,\n"
        "  key_assumptions: [] (3+), testable_predictions: [] (3+),\n"
        "  baseline_comparison, expected_improvement (quantitative),\n"
        "  key_risks: [{risk, likelihood, mitigation}] (3+),\n"
        "  kill_criteria: [] (3+), minimum_viable_experiment, estimated_compute_budget"
    ),
    Stage.ANALYSIS: (
        "Output format (YAML in ```yaml fences):\n"
        "  hypothesis_recap, experiments_run,\n"
        "  key_results: [{experiment, metric, value, baseline_value, improvement}],\n"
        "  statistical_significance, ablation_findings,\n"
        "  claims_supported, claims_not_supported,\n"
        "  alternative_explanations, limitations (2+), future_work, conclusion"
    ),
}


class PipelineRunner:
    """Runs pipeline operations in background threads, exposes status via API."""

    def __init__(self, sm: StateManager, base_dir: Path, config: dict):
        self.sm = sm
        self.base_dir = base_dir
        self.config = config
        self._dispatcher: Optional[MultiAgentDispatcher] = None
        self._cfg_ver = 0
        self._cur_cfg_ver = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._approval = threading.Event()
        # Status
        self.running = False
        self.mode = ""          # "auto", "step", "review"
        self.stage_label = ""
        self.waiting_approval = False
        self.log_lines: list[dict] = []

    @property
    def project_id(self) -> Optional[str]:
        f = self.base_dir / ".active_project"
        return f.read_text("utf-8").strip() if f.exists() else None

    @project_id.setter
    def project_id(self, pid: str):
        (self.base_dir / ".active_project").write_text(pid, encoding="utf-8")

    def _get_dispatcher(self) -> MultiAgentDispatcher:
        if self._dispatcher is None or self._cfg_ver != self._cur_cfg_ver:
            self._dispatcher = MultiAgentDispatcher(
                self.base_dir, self.base_dir / "agents", self.config)
            self._cfg_ver = self._cur_cfg_ver
        return self._dispatcher

    def reload_config(self, config: dict, silent: bool = False):
        self.config = config
        self._cur_cfg_ver += 1
        # Don't null dispatcher while pipeline is running — it will pick up
        # the new config version on the next _get_dispatcher() call automatically
        if not self.running:
            self._dispatcher = None
        if not silent:
            self.log(f"Config updated. New settings apply to next step.")

    def log(self, msg: str):
        self.log_lines.append({"t": datetime.now().strftime("%H:%M:%S"), "m": msg})
        if len(self.log_lines) > 2000:
            self.log_lines = self.log_lines[-2000:]

    def get_status(self) -> dict:
        return {
            "running": self.running, "mode": self.mode,
            "stage": self.stage_label, "waiting_approval": self.waiting_approval,
            "log": self.log_lines[-500:],
        }

    # --- Public actions ---

    def create_project(self, name: str, question: str) -> str:
        state = self.sm.create_project(name, "", question)
        self.project_id = state.project_id
        self.log(f"Created project: {state.name} ({state.project_id})")
        return state.project_id

    def start_auto(self, until: str = "", max_rev: int = 3, instruction: str = ""):
        if self.running:
            return
        until_stage = Stage(until) if until else None
        self._start_thread("auto", self._run_auto, until_stage, max_rev, instruction)

    def start_step(self, instruction: str = ""):
        if self.running:
            return
        self._start_thread("step", self._run_step_and_review, instruction)

    def start_review(self):
        if self.running:
            return
        self._start_thread("review", self._do_review)

    def approve(self, feedback: str = ""):
        if self.waiting_approval:
            self._approval_feedback = feedback
            self._approval.set()

    def reject(self, feedback: str = ""):
        """Reject at human gate — record event and keep pipeline paused."""
        if not self.waiting_approval:
            return
        pid = self.project_id
        if pid:
            state = self.sm.load_project(pid)
            state.record_event(VersionEventType.HUMAN_REJECT,
                               f"Human rejected: {feedback[:80]}" if feedback else "Human rejected",
                               detail=feedback)
            self.sm.save_project(state)
        self.log(f"Rejected: {feedback[:100]}" if feedback else "Rejected by user.")
        self.waiting_approval = False
        self._stop.set()
        self._approval.set()

    def stop(self):
        self._stop.set()
        self._approval.set()  # unblock if waiting
        # Force-kill running dispatcher subprocess if any
        d = self._dispatcher
        if d and hasattr(d, '_active_proc') and d._active_proc is not None:
            try:
                import signal
                os.killpg(os.getpgid(d._active_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    d._active_proc.kill()
                except Exception:
                    pass
            self.log("  Force-killed running agent process.")

    def _start_thread(self, mode, target, *args):
        self.running = True
        self.mode = mode
        self._stop.clear()
        self._approval.clear()
        self.waiting_approval = False
        self.log_lines = []
        self._thread = threading.Thread(target=self._safe_run, args=(target, *args), daemon=True)
        self._thread.start()

    def _safe_run(self, target, *args):
        try:
            target(*args)
        except Exception as e:
            self.log(f"ERROR: {type(e).__name__}: {e}")
        finally:
            self.running = False
            self.mode = ""
            self.stage_label = ""
            self.waiting_approval = False

    # --- Pipeline logic (adapted from multi_agent.py) ---

    def _build_task(self, state, stage, role, instruction="", feedback=""):
        pid = self.project_id
        task_id = f"{stage.value}-{uuid.uuid4().hex[:6]}"
        art_dir = f"projects/{pid}/artifacts/{stage.value}"

        ctx = []
        seen = set()
        si = STAGE_ORDER.index(stage)
        for s in STAGE_ORDER[:si + 1]:
            for at in STAGE_REQUIRED_ARTIFACTS.get(s, []):
                latest = state.latest_artifact(at)
                if latest and at.value not in seen:
                    ctx.append(f"projects/{pid}/{latest.path}")
                    seen.add(at.value)

        outputs = []
        for at in STAGE_REQUIRED_ARTIFACTS.get(stage, []):
            existing = [a for a in state.artifacts if a.artifact_type == at]
            v = max((a.version for a in existing), default=0) + 1
            outputs.append(f"{art_dir}/{at.value}_v{v}.yaml")

        if not instruction:
            instruction = self._default_instr(stage, state)

        return TaskCard(
            task_id=task_id, role=role, stage=stage, instruction=instruction,
            context_files=ctx, required_outputs=outputs, previous_feedback=feedback,
            constraints=[f"Project: {state.name}", f"Question: {state.research_question}",
                         f"Iteration: {state.iteration_count.get(stage.value, 1)}",
                         f"Write to: {art_dir}/"],
            metadata={"project_id": pid, "iteration": state.iteration_count.get(stage.value, 1)},
        )

    @staticmethod
    def _default_instr(stage, state):
        q = state.research_question or "the research question"
        # Include YAML format spec per stage (keeps CLAUDE.md short)
        fmt = _STAGE_YAML_FORMATS.get(stage, "")
        base = {
            Stage.PROBLEM_DEFINITION: f"Define the research problem for: {q}. Include 5+ references.",
            Stage.LITERATURE_REVIEW: "Thorough literature review. Read problem_brief. Find 10+ papers, gaps, baselines.",
            Stage.HYPOTHESIS_FORMATION: "Formulate testable hypothesis. Kill criteria are critical.",
            Stage.EXPERIMENT_DESIGN: "Design complete experiment. Read hypothesis_card. Baselines + ablations.",
            Stage.IMPLEMENTATION: "Implement experiment per spec. Single command, reproducible, with tests.",
            Stage.EXPERIMENTATION: "Verify experiment ready. Smoke test must pass.",
            Stage.ANALYSIS: "Analyze results. Cite specific experiments for every claim.",
        }.get(stage, "Proceed.")
        return f"{base}\n\n{fmt}" if fmt else base

    def _do_step(self, instruction="") -> AgentResult:
        pid = self.project_id
        state = self.sm.load_project(pid)
        stage = state.current_stage
        role = STAGE_PRIMARY_AGENT[stage]
        d = self._get_dispatcher()
        self.stage_label = stage.value

        fb = ""
        gates = [g for g in state.gate_results if g.stage == stage]
        if gates and gates[-1].status == GateStatus.FAILED:
            fb = gates[-1].overall_feedback

        task = self._build_task(state, stage, role, instruction, fb)

        backend = d.backends.get(role, CLIBackend.CLAUDE)
        self.log(f"┌─ v{state.current_version()} {role.value} agent")
        self.log(f"│  {stage.value} | {backend.value} | {d.models.get(role, '?')}")
        self.log(f"└─ Running...")

        result = d.dispatch(task, progress_fn=self.log, cancel_event=self._stop)

        if result.is_auth_error:
            self.log(f"AUTH ERROR — pipeline paused. Fix credentials and retry.")
            return result

        icon = "✓" if result.success else "✗"
        retry = f" (retried {result.retries}x)" if result.retries else ""
        self.log(f"┌─ {icon} Done ({result.duration_seconds:.1f}s){retry}")
        self.log(f"└─ Files: {result.output_files}")

        # --- Validate and register artifacts BEFORE recording the event ---
        state = self.sm.load_project(pid)
        project_dir = self.base_dir / "projects" / pid
        registered_count = 0
        registered_types: set[ArtifactType] = set()
        accepted_files: list[str] = []

        from .artifacts import load_schema, validate_artifact_content, register_artifact_file
        for p in result.output_files:
            for at in ArtifactType:
                if at.value in Path(p).stem:
                    op = Path(p)
                    if op.is_absolute():
                        actual = op
                    elif str(op).startswith("projects/"):
                        actual = self.base_dir / op
                    else:
                        actual = project_dir / op

                    if not actual.exists():
                        self.log(f"  SKIP: {at.value} — file not found: {actual}")
                        break

                    try:
                        raw = actual.read_text(encoding="utf-8")
                        yaml.safe_load(raw)
                    except yaml.YAMLError as e:
                        self.log(f"  REJECT: {at.value} — invalid YAML: {e}")
                        break

                    schema = load_schema(self.base_dir / "schemas", at)
                    if schema:
                        errors = validate_artifact_content(raw, schema)
                        if errors:
                            self.log(f"  REJECT: {at.value} — schema errors: {errors[:3]}")
                            break

                    art = register_artifact_file(
                        state, at, stage, role, actual, project_dir,
                        metadata={
                            "backend": d.backends.get(role, CLIBackend.CLAUDE).value,
                            "model": d.models.get(role, "unknown"),
                            "duration_seconds": result.duration_seconds,
                            "exit_code": result.exit_code,
                            "iteration": state.current_iteration(),
                        },
                    )
                    registered_count += 1
                    registered_types.add(at)
                    # Use canonical path (register may have renamed the file)
                    accepted_files.append(f"projects/{pid}/{art.path}")
                    break

        # --- Post-validation: update result to reflect what was actually registered ---

        if result.output_files and registered_count == 0:
            result.success = False
            result.output_files = []
            result.error = "All output files rejected by validation"
            self.log(f"  All output file(s) rejected — step marked failed")
        else:
            result.output_files = accepted_files

        # Check THIS dispatch's completeness (not historical artifacts)
        required_types_list = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
        if required_types_list and registered_count > 0:
            missing = [at.value for at in required_types_list if at not in registered_types]
            if missing:
                result.success = False
                result.error = f"Missing required artifacts: {missing}"
                self.log(f"  Incomplete: missing {missing} — proceeding to review for feedback")

        # --- NOW record event with accurate post-validation data ---
        backend = d.backends.get(role, CLIBackend.CLAUDE)
        summary = f"{role.value} ({backend.value}) → {', '.join(Path(p).name for p in accepted_files) or 'no valid artifacts'}"
        state.record_event(
            VersionEventType.AGENT_RUN,
            summary,
            agent=role, artifacts_produced=accepted_files,
            cost_usd=result.cost_usd, duration_seconds=result.duration_seconds,
            detail=result.output_text,
        )
        # Record cost
        if result.cost_usd > 0:
            backend_enum = d.backends.get(role, CLIBackend.CLAUDE)
            _pmap = {CLIBackend.CLAUDE: LLMProvider.CLAUDE, CLIBackend.CODEX: LLMProvider.CODEX,
                     CLIBackend.OPENCODE: LLMProvider.OPENCODE}
            cost_desc = f"{role.value}/{stage.value}"
            if result.cost_source == "estimated":
                cost_desc += " (estimated)"
            state.cost_records.append(CostRecord(
                agent=role, provider=_pmap.get(backend_enum, LLMProvider.CLAUDE),
                model=d.models.get(role, "unknown"),
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cost_usd=result.cost_usd, task_description=cost_desc, stage=stage,
            ))

        self.sm.save_project(state)
        return result

    def _materialize_code(self, state) -> list[str]:
        """Extract code from YAML code artifacts into actual files."""
        return shared_materialize_code(
            state, self.sm, self.project_id, self.base_dir, log_fn=self.log,
        )

    def _execute_experiment(self, state) -> tuple[Optional[str], int]:
        """Run the smoke test from run_manifest and capture output."""
        return shared_execute_experiment(
            state, self.sm, self.project_id, self.base_dir, log_fn=self.log,
        )

    def _pre_review_checks(self, state, stage) -> list[str]:
        """Automated structural checks — delegates to shared prechecks module."""
        return shared_pre_review_checks(
            state, stage, self.sm, self.project_id, self.base_dir,
        )

    def _run_orchestrator_validation(self, stage: Stage) -> dict:
        """Orchestrator step: validate, materialize, execute BEFORE critic review."""
        pid = self.project_id
        state = self.sm.load_project(pid)

        if stage == Stage.IMPLEMENTATION:
            self.log("  [Orchestrator] Materializing code and running tests...")
            return run_and_record_tests(
                state, self.sm, pid, self.base_dir, log_fn=self.log)

        if stage == Stage.EXPERIMENTATION:
            self.log("  [Orchestrator] Executing experiment and recording metrics...")
            return run_and_record_experiment(
                state, self.sm, pid, self.base_dir, log_fn=self.log)

        return {"success": True}

    def _do_review(self) -> Optional[GateResult]:
        pid = self.project_id
        state = self.sm.load_project(pid)
        stage = state.current_stage
        d = self._get_dispatcher()
        self.stage_label = stage.value

        # Run automated structural pre-checks
        pre_issues = self._pre_review_checks(state, stage)
        if pre_issues:
            self.log(f"  Pre-check issues found:")
            for iss in pre_issues:
                self.log(f"    - {iss}")

        # Build a proper review instruction with criteria and verdict format
        from .agents.critic import STAGE_REVIEW_CRITERIA
        criteria = STAGE_REVIEW_CRITERIA.get(stage.value, "Review for scientific rigor.")

        # Read ONLY the latest version of each artifact type for review
        art_summaries = []
        seen_types: set[str] = set()
        for a in reversed(state.stage_artifacts(stage)):
            if a.artifact_type.value in seen_types:
                continue  # Skip older versions — critic should only see latest
            seen_types.add(a.artifact_type.value)
            try:
                content = self.sm.read_artifact_file(pid, a)
                art_summaries.append(f"### {a.artifact_type.value} (v{a.version} — latest)\n```yaml\n{content[:3000]}\n```")
            except Exception:
                pass
        art_summaries.reverse()  # Restore chronological order for readability

        # Include pre-check issues in the review prompt so critic is aware
        pre_check_section = ""
        if pre_issues:
            pre_check_section = (
                "\n\n## Automated Pre-Check Issues (MUST address in review)\n"
                + "\n".join(f"- BLOCKING: {iss}" for iss in pre_issues)
                + "\nThese issues were detected by automated checks. If they are valid, your verdict MUST NOT be PASS.\n"
            )

        # Build stage-specific score keys from stages.yaml if available
        score_keys = "rigor, completeness, clarity, novelty"  # default
        stages_cfg_file = self.base_dir / "config" / "stages.yaml"
        stages_cfg = {}
        if stages_cfg_file.exists():
            try:
                stages_cfg = yaml.safe_load(stages_cfg_file.read_text()) or {}
                stage_criteria = stages_cfg.get("stages", {}).get(stage.value, {}).get("gate_criteria", [])
                if stage_criteria:
                    score_keys = ", ".join(c["name"] for c in stage_criteria if "name" in c)
            except Exception:
                pass

        orchestrator_note = ""
        if stage in (Stage.IMPLEMENTATION, Stage.EXPERIMENTATION):
            orchestrator_note = (
                "\n\n## Orchestrator Execution Results\n"
                "The test_result and metrics artifacts have been VERIFIED by the Orchestrator "
                "through actual code execution. Review the ACTUAL results, not agent claims.\n"
            )

        review_instruction = (
            f"CRITICAL RULES:\n"
            f"- You are a REVIEWER. Do NOT write files. Do NOT create v2 artifacts.\n"
            f"- ONLY output a review YAML block in your response.\n\n"
            f"Review the {stage.value} artifacts below.\n\n"
            f"## Review Criteria\n{criteria}\n\n"
            f"## Artifacts to Review\n" + "\n\n".join(art_summaries) + "\n\n"
            + orchestrator_note
            + f"## Required Output (print this YAML, do NOT write any files)\n"
            f"```yaml\n"
            f"verdict: PASS | REVISE | FAIL\n"
            f"failure_type: (required if REVISE/FAIL) structural_issue | implementation_bug | "
            f"design_flaw | hypothesis_needs_revision | evidence_insufficient | "
            f"hypothesis_falsified | analysis_gap\n"
            f"scores:  # score each 0.0-1.0\n"
            + "".join(f"  {k.strip()}: 0.0-1.0\n" for k in score_keys.split(","))
            + f"blocking_issues: []\n"
            f"suggestions: []\n"
            f"strongest_objection: \"\"\n"
            f"what_would_make_it_pass: \"\"\n"
            f"```\n"
            f"PASS only if ALL scores >= 0.7 AND no blocking issues.\n"
            + pre_check_section
        )

        task = self._build_task(state, stage, AgentRole.CRITIC, review_instruction)

        cb = d.backends.get(AgentRole.CRITIC, CLIBackend.CODEX)
        self.log(f"┌─ v{state.current_version()} Critic ({cb.value}/{d.models.get(AgentRole.CRITIC, '?')})")
        self.log(f"└─ Reviewing {stage.value}...")

        result = d.dispatch(task, progress_fn=self.log)

        if result.is_auth_error or (not result.success and not result.output_text.strip()):
            self.log(f"Critic failed: {result.error or 'no output'}")
            state = self.sm.load_project(pid)
            gr = GateResult(
                gate_name=f"{stage.value}_review", stage=stage, status=GateStatus.FAILED,
                checks=[GateCheck(name="error", description="Critic unreachable",
                                  check_type="codex", passed=False, feedback="Network/auth error")],
                reviewer=AgentRole.CRITIC,
                overall_feedback="Critic review failed. Re-run when connection is restored.",
                iteration=state.iteration_count.get(stage.value, 1),
            )
            state.gate_results.append(gr)
            state.record_event(VersionEventType.GATE_FAILED, "Critic unavailable",
                               agent=AgentRole.CRITIC, detail=result.output_text[:500])
            self.sm.save_project(state)
            return gr

        # --- Layered gate evaluation (shared module) ---
        stage_gate = stages_cfg.get("stages", {}).get(stage.value, {}) if stages_cfg else {}
        stage_criteria = stage_gate.get("gate_criteria", [])
        threshold = stage_gate.get("pass_threshold", 0.7)

        gv = evaluate_gate_verdict(
            result.output_text, result.success,
            pre_issues, stage_criteria, threshold,
        )
        verdict = gv.verdict

        if gv.pre_check_override:
            self.log(f"  Pre-check override: PASS → REVISE ({len(pre_issues)} blocking issue(s))")
            result.output_text += gv.annotation
        if gv.score_override:
            self.log(f"  Weighted score {gv.weighted_avg:.2f} < {threshold} — PASS → REVISE")

        gr = GateResult(
            gate_name=f"{stage.value}_review", stage=stage,
            status=GateStatus.PASSED if verdict == "PASS" else GateStatus.FAILED,
            checks=[GateCheck(name="review", description="Adversarial review",
                              check_type="codex", passed=verdict == "PASS",
                              feedback=result.output_text)],
            reviewer=AgentRole.CRITIC,
            overall_feedback=result.output_text,
            iteration=state.iteration_count.get(stage.value, 1),
        )

        state = self.sm.load_project(pid)
        state.gate_results.append(gr)
        evt = VersionEventType.GATE_PASSED if verdict == "PASS" else VersionEventType.GATE_FAILED
        state.record_event(evt, f"Critic: {verdict}", agent=AgentRole.CRITIC,
                           gate_verdict=verdict, duration_seconds=result.duration_seconds,
                           detail=result.output_text)
        # Record review cost
        if result.cost_usd > 0:
            backend_enum = d.backends.get(AgentRole.CRITIC, CLIBackend.CODEX)
            _pmap = {CLIBackend.CLAUDE: LLMProvider.CLAUDE, CLIBackend.CODEX: LLMProvider.CODEX,
                     CLIBackend.OPENCODE: LLMProvider.OPENCODE}
            cost_desc = f"critic/{stage.value}"
            if result.cost_source == "estimated":
                cost_desc += " (estimated)"
            state.cost_records.append(CostRecord(
                agent=AgentRole.CRITIC, provider=_pmap.get(backend_enum, LLMProvider.CLAUDE),
                model=d.models.get(AgentRole.CRITIC, "unknown"),
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cost_usd=result.cost_usd, task_description=cost_desc, stage=stage,
            ))
        self.sm.save_project(state)

        icon = "✓" if verdict == "PASS" else "✗"
        self.log(f"  {icon} Critic: {verdict} ({result.duration_seconds:.1f}s)")
        return gr

    def _run_step_and_review(self, instruction=""):
        """Run one step + review (step mode)."""
        result = self._do_step(instruction)
        if not result.success and not result.output_files:
            self.log("Agent produced no output.")
            return

        # Orchestrator validates + executes (BEFORE critic review)
        state = self.sm.load_project(self.project_id)
        stage = state.current_stage
        self._run_orchestrator_validation(stage)

        self._do_review()

    def _run_auto(self, until_stage, max_rev, instruction):
        """Full auto mode."""
        pid = self.project_id
        human_gates = [Stage(s) for s in
                       self.config.get("pipeline", {}).get("human_gates",
                       ["hypothesis_formation", "experimentation"])]

        while not self._stop.is_set():
            state = self.sm.load_project(pid)
            stage = state.current_stage

            if until_stage and STAGE_ORDER.index(stage) > STAGE_ORDER.index(until_stage):
                self.log(f"Reached {until_stage.value}. Stopping.")
                break
            if stage == STAGE_ORDER[-1]:
                gates = [g for g in state.gate_results if g.stage == stage]
                if gates and gates[-1].status == GateStatus.PASSED:
                    self.log("Pipeline complete!")
                    break

            self.log(f"\n{'='*50}")
            self.log(f"  v{state.current_version()}  STAGE: {stage.value}")
            self.log(f"{'='*50}")

            for rev in range(max_rev + 1):
                if self._stop.is_set():
                    self.log("Stopped by user.")
                    return

                # 1. Agent produces artifacts
                result = self._do_step(instruction)
                instruction = ""

                if result.is_auth_error:
                    self.log("Auth error — pipeline paused.")
                    return

                if not result.success and not result.output_files:
                    self.log("No output, retrying...")
                    continue

                # 2. Orchestrator validates + executes (BEFORE critic review)
                self._run_orchestrator_validation(stage)

                # 3. Critic reviews ACTUAL results
                gr = self._do_review()
                if gr and gr.status == GateStatus.PASSED:
                    break

                # 4. Check failure_type for cross-stage rollback
                ft = parse_failure_type(gr.overall_feedback if gr else "")
                if ft and ft in FAILURE_TYPE_CROSS_STAGE:
                    break  # Exit inner loop — outer loop handles rollback

                # Same-stage revise
                if rev < max_rev:
                    state = self.sm.load_project(pid)
                    state.increment_iteration()
                    self.sm.save_project(state)
                    self.log(f"  Revision {rev+1}/{max_rev} → v{state.current_version()}")

            # Post-revision: check gate
            state = self.sm.load_project(pid)
            gates = [g for g in state.gate_results if g.stage == state.current_stage]
            latest = gates[-1] if gates else None

            if not latest or latest.status != GateStatus.PASSED:
                # --- Automatic backward transition evaluation ---
                max_iters = self.config.get("pipeline", {}).get("max_iterations", 5)
                rollback_target = evaluate_rollback(
                    state, stage, latest, max_iters,
                    state_manager=self.sm, project_id=pid,
                )
                if rollback_target:
                    trigger = ALLOWED_TRANSITIONS.get(
                        (stage, rollback_target), "auto_rollback"
                    )
                    state.record_transition(
                        rollback_target, trigger, gate_result=latest,
                        notes=f"Auto-rollback from {stage.value}",
                    )
                    self.sm.save_project(state)
                    self.log(f"  ↩ Auto-rollback: {stage.value} → {rollback_target.value}")
                    continue  # Re-enter while loop at rolled-back stage
                self.log(f"Gate not passed for {stage.value}. Paused.")
                break

            # Human gate?
            if stage in human_gates:
                self.waiting_approval = True
                self.stage_label = f"{stage.value} (awaiting approval)"
                state = self.sm.load_project(pid)
                if gates:
                    gates[-1].status = GateStatus.HUMAN_REVIEW
                state.record_event(VersionEventType.GATE_REVIEW,
                                   f"Human gate: {stage.value}")
                self.sm.save_project(state)
                self.log(f"Human gate at {stage.value}. Click Approve to continue.")

                self._approval.wait()
                self._approval.clear()
                self.waiting_approval = False

                if self._stop.is_set():
                    self.log("Stopped by user.")
                    return

                fb = getattr(self, '_approval_feedback', '') or ''
                self._approval_feedback = ''
                self.log(f"Approved!{' Feedback: ' + fb[:80] if fb else ''} Advancing...")
                state = self.sm.load_project(pid)
                state.record_event(VersionEventType.HUMAN_APPROVE,
                                   f"Human approved{': ' + fb[:80] if fb else ''}",
                                   detail=fb)
                self.sm.save_project(state)

            # Pre-advance completeness gate: iteration-aware
            state = self.sm.load_project(pid)
            cur_iter = state.current_iteration()
            required = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
            advance_missing = []
            for at in required:
                lat = state.latest_artifact(at)
                if not lat:
                    advance_missing.append(at.value)
                elif lat.metadata.get("iteration", 0) < cur_iter:
                    advance_missing.append(f"{at.value} (stale)")
            if advance_missing:
                self.log(f"  Cannot advance: missing/stale artifacts {advance_missing}")
                break

            # Advance
            idx = STAGE_ORDER.index(stage)
            if idx < len(STAGE_ORDER) - 1:
                nxt = STAGE_ORDER[idx + 1]
                state = self.sm.load_project(pid)
                trigger = ALLOWED_TRANSITIONS.get((stage, nxt), "auto_advance")
                state.record_transition(nxt, trigger, gate_result=latest)
                self.sm.save_project(state)
                self.log(f">>> v{state.current_version()} Advanced → {nxt.value}")
            else:
                self.log("Pipeline complete!")
                break


# ---------------------------------------------------------------------------
# OpenCode model discovery
# ---------------------------------------------------------------------------

def _get_opencode_models() -> list[str]:
    import subprocess, os
    opencode_bin = os.environ.get("OPENCODE_BIN", os.path.expanduser("~/.opencode/bin/opencode"))
    try:
        r = subprocess.run([opencode_bin, "models"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
    except Exception:
        pass
    return ["volcengine-plan/doubao-seed-2.0-pro", "volcengine-plan/deepseek-v3.2"]


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research Agent</title>
<style>
:root{--bg:#0d1117;--sf:#161b22;--bd:#30363d;--tx:#e6edf3;--dim:#8b949e;--br:#f0f6fc;
--gr:#3fb950;--rd:#f85149;--yl:#d29922;--bl:#58a6ff;--pu:#bc8cff;--cy:#39d2c0;--or:#f0883e}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--tx);line-height:1.5;font-size:13px}
button{font-family:inherit;cursor:pointer;font-size:12px}
button:disabled{opacity:.35;cursor:not-allowed}
select,input{font-family:inherit;background:var(--sf);border:1px solid var(--bd);color:var(--tx);padding:4px 8px;border-radius:5px;font-size:12px}
select:focus,input:focus{border-color:var(--bl);outline:none}

/* === LAYOUT: sidebar + content === */
.app{display:flex;height:100vh;overflow:hidden}

/* Left sidebar — project list (Claude.ai style) */
.sidebar{width:260px;background:var(--sf);border-right:1px solid var(--bd);display:flex;flex-direction:column;flex-shrink:0}
.sb-hdr{padding:14px 16px 10px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between}
.sb-hdr h2{font-size:14px;color:var(--br)}
.sb-new{background:var(--bl);color:var(--bg);border:none;padding:4px 12px;border-radius:5px;font-weight:600;font-size:11px}
.sb-new:hover{opacity:.85}
.sb-list{flex:1;overflow-y:auto;padding:6px 0}
.sb-item{padding:10px 16px;cursor:pointer;display:flex;align-items:center;gap:10px;border-left:3px solid transparent;transition:.1s}
.sb-item:hover{background:rgba(255,255,255,.04)}
.sb-item.active{background:rgba(88,166,255,.1);border-left-color:var(--bl)}
.sb-item .pi-info{flex:1;min-width:0}
.sb-item .pi-name{font-size:12px;font-weight:500;color:var(--tx);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-item .pi-stage{font-size:10px;color:var(--dim)}
.sb-item .pi-del{opacity:0;background:none;border:none;color:var(--dim);font-size:14px;padding:0 4px;line-height:1}
.sb-item:hover .pi-del{opacity:.6}
.sb-item .pi-del:hover{color:var(--rd);opacity:1}

/* Spinner */
.spinner{width:14px;height:14px;border:2px solid var(--bd);border-top-color:var(--bl);border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

/* Right content */
.content{flex:1;display:flex;flex-direction:column;overflow:hidden;padding:0 20px 12px}

/* Header */
.hdr{display:flex;align-items:center;gap:10px;padding:12px 0 8px;flex-wrap:wrap}
.hdr h1{font-size:16px;color:var(--br);flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{padding:2px 10px;border-radius:10px;font-size:11px;font-weight:600}
.badge-v{background:var(--bl);color:var(--bg)}
.badge-cost{color:var(--yl)}
.btn{background:var(--sf);border:1px solid var(--bd);color:var(--dim);padding:4px 12px;border-radius:5px;font-size:12px}
.btn:hover:not(:disabled){border-color:var(--bl);color:var(--tx)}
.btn.active{background:var(--bl);color:var(--bg);border-color:var(--bl)}

/* Overview panel */
.overview{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;margin:6px 0}
.ov-q{font-size:13px;color:var(--cy);line-height:1.6;margin-bottom:10px;border-left:3px solid var(--cy);padding-left:10px}
.ov-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.ov-card{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:8px 10px}
.ov-label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.ov-val{font-size:18px;font-weight:600;color:var(--br);margin-top:2px}
.ov-val.sm{font-size:13px}
.ov-stage-desc{font-size:11px;color:var(--dim);margin-top:8px;padding:6px 10px;background:var(--bg);border-radius:4px;border-left:2px solid var(--bl)}

/* Stats panel */
.stat-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:11px}
.stat-lbl{width:120px;color:var(--dim);text-align:right;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stat-bar{flex:1;height:16px;background:var(--bg);border-radius:3px;overflow:hidden;position:relative}
.stat-fill{height:100%;border-radius:3px;transition:width .3s}
.stat-val{width:60px;font-size:10px;color:var(--dim);text-align:right;flex-shrink:0}

/* Control bar */
.ctrl{display:flex;align-items:center;gap:8px;padding:8px 0;border-top:1px solid var(--bd);border-bottom:1px solid var(--bd);flex-wrap:wrap}
.ctrl label{color:var(--dim);font-size:11px}
.btn-run{background:var(--gr);color:var(--bg);border:none;padding:5px 16px;border-radius:5px;font-weight:600}
.btn-run:hover:not(:disabled){opacity:.85}
.btn-stop{background:var(--rd);color:#fff;border:none;padding:5px 12px;border-radius:5px;font-weight:600}
.btn-approve{background:var(--yl);color:var(--bg);border:none;padding:5px 14px;border-radius:5px;font-weight:600}
.status-pill{display:flex;align-items:center;gap:6px;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:500}
.status-pill.idle{background:rgba(139,148,158,.15);color:var(--dim)}
.status-pill.running{background:rgba(63,185,80,.15);color:var(--gr)}
.status-pill.waiting{background:rgba(210,153,34,.15);color:var(--yl)}

/* Settings */
.panel{display:none;background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:14px;margin:8px 0}
.panel.vis{display:block}
.panel h3{font-size:13px;color:var(--br);margin-bottom:10px}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
.acard{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:10px}
.acard h4{font-size:12px;margin-bottom:6px;display:flex;align-items:center;gap:5px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot-researcher{background:var(--bl)}.dot-engineer{background:var(--gr)}.dot-critic{background:var(--pu)}.dot-orchestrator{background:var(--or)}
.crow{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.crow label{font-size:11px;color:var(--dim);min-width:50px}
.crow select{flex:1}

/* Stages bar */
.stages{display:flex;gap:3px;padding:8px 0}
.schip{flex:1;padding:6px 4px;border-radius:6px;text-align:center;font-size:10px;background:var(--sf);border:1px solid var(--bd)}
.schip.done{background:#0d2818;border-color:var(--gr);color:var(--gr)}
.schip.active{background:#1a1f35;border-color:var(--bl);color:var(--bl);box-shadow:0 0 6px rgba(88,166,255,.2)}
.schip.failed{background:#2d1215;border-color:var(--rd);color:var(--rd)}
.schip .al{display:block;font-size:8px;color:var(--dim);margin-top:1px}
.schip .iter{font-size:9px;background:var(--yl);color:var(--bg);padding:0 4px;border-radius:3px;font-weight:700;margin-left:2px}

/* Timeline + Detail */
.main{display:grid;grid-template-columns:220px 1fr;gap:10px;flex:1;min-height:0;overflow:hidden;padding-top:6px}
.side{background:var(--sf);border:1px solid var(--bd);border-radius:6px;display:flex;flex-direction:column;overflow:hidden}
.side h3{padding:8px 12px;font-size:11px;color:var(--dim);border-bottom:1px solid var(--bd);flex-shrink:0}
.side-scroll{flex:1;overflow-y:auto}
.vg{border-bottom:1px solid var(--bd)}
.vh{padding:7px 12px;font-size:11px;font-weight:600;color:var(--br);cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.vh:hover{background:rgba(88,166,255,.05)}
.vh.sel{background:rgba(88,166,255,.1);border-left:3px solid var(--bl)}
.stag{font-size:8px;padding:1px 5px;border-radius:3px;background:var(--bd);color:var(--dim);white-space:nowrap}
.ves{padding:0 12px 4px}
.ve{padding:2px 0;font-size:10px;color:var(--dim);display:flex;align-items:center;gap:4px}
.ve .ico{width:12px;text-align:center;flex-shrink:0}

.det{background:var(--sf);border:1px solid var(--bd);border-radius:6px;display:flex;flex-direction:column;overflow:hidden}
.det-hdr{padding:10px 14px;border-bottom:1px solid var(--bd);flex-shrink:0;display:flex;justify-content:space-between;align-items:center}
.det-hdr h2{font-size:14px;color:var(--br)}
.det-scroll{flex:1;overflow-y:auto;padding:10px 14px}

.ec{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:12px;margin-bottom:8px}
.ec-hdr{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px;flex-wrap:wrap;gap:3px}
.ab{font-size:10px;padding:2px 8px;border-radius:8px;font-weight:600;white-space:nowrap}
.ab-researcher{background:#1a2744;color:var(--bl)}.ab-critic{background:#2a1a2e;color:var(--pu)}
.ab-engineer{background:#1a2e1e;color:var(--gr)}.ab-human{background:#2e2a1a;color:var(--yl)}
.ev-s{font-size:12px;flex:1;min-width:0}.ev-m{font-size:10px;color:var(--dim);white-space:nowrap}
.vd{font-weight:600}.vd-PASS{color:var(--gr)}.vd-FAIL{color:var(--rd)}.vd-REVISE{color:var(--yl)}
.scores{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.sc{font-size:10px;padding:1px 6px;border-radius:3px;background:var(--bg);border:1px solid var(--bd)}
.sc.p{border-color:var(--gr);color:var(--gr)}.sc.f{border-color:var(--rd);color:var(--rd)}
.al-list{list-style:none;margin-top:4px}
.al-list li{font-size:10px;padding:1px 0;color:var(--cy)}
.al-list li::before{content:"\1F4C4 "}
.dtxt{font-size:11px;white-space:pre-wrap;word-break:break-word;color:var(--tx);background:var(--bg);padding:8px;border-radius:4px;border:1px solid var(--bd);margin-top:6px;max-height:300px;overflow-y:auto;transition:max-height .3s;font-family:'SF Mono',Consolas,monospace}
.dtxt.exp{max-height:none}
.dtog{font-size:10px;color:var(--bl);cursor:pointer;margin-top:2px;user-select:none}

/* Console */
.console{background:#000;border:1px solid var(--bd);border-radius:6px;height:200px;min-height:80px;max-height:60vh;resize:vertical;display:flex;flex-direction:column;margin-top:6px;overflow:hidden}
.console-hdr{display:flex;justify-content:space-between;align-items:center;padding:4px 10px;border-bottom:1px solid var(--bd);flex-shrink:0}
.console-hdr span{font-size:11px;color:var(--dim)}
.con-body{flex:1;overflow-y:auto;padding:6px 10px;font-family:'SF Mono',Consolas,monospace;font-size:11px;color:var(--gr);line-height:1.4}
.con-body .err{color:var(--rd)}

/* Modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;justify-content:center;align-items:center}
.modal-bg.vis{display:flex}
.modal{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:24px;width:420px;max-width:90vw}
.modal h3{font-size:15px;color:var(--br);margin-bottom:14px}
.modal input{width:100%;margin-bottom:10px;padding:10px;font-size:13px;border-radius:6px}
.modal .btns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}

/* Full-screen viewer modal */
.viewer{background:var(--sf);border:1px solid var(--bd);border-radius:10px;width:90vw;height:85vh;display:flex;flex-direction:column}
.viewer-hdr{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--bd);flex-shrink:0}
.viewer-hdr h3{font-size:14px;color:var(--br);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.viewer-body{flex:1;overflow:auto;padding:14px 16px;font-family:'SF Mono',Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;color:var(--tx);margin:0;line-height:1.5}
.btn-view{background:none;border:1px solid var(--bd);color:var(--bl);padding:1px 6px;border-radius:3px;font-size:10px;cursor:pointer;margin-left:4px}
.btn-view:hover{border-color:var(--bl);background:rgba(88,166,255,.1)}

@media(max-width:900px){.sidebar{width:200px}.main{grid-template-columns:1fr}.stages{flex-wrap:wrap}}
@media(max-width:640px){.sidebar{display:none}.content{padding:0 10px 8px}}
</style></head>
<body>
<div class="app">
  <!-- ===== LEFT SIDEBAR — Project List ===== -->
  <div class="sidebar">
    <div class="sb-hdr">
      <h2>Projects</h2>
      <button class="sb-new" onclick="showModal()">+ New</button>
    </div>
    <div class="sb-list" id="sbList"></div>
  </div>

  <!-- ===== MAIN CONTENT ===== -->
  <div class="content">
    <!-- Header -->
    <div class="hdr">
      <h1 id="projName"></h1>
      <span class="badge badge-cost" id="costLabel"></span>
      <span class="badge badge-v" id="verLabel"></span>
      <button class="btn" id="statsBtn" onclick="togglePanel('statsPanel',this)">Stats</button>
      <button class="btn" id="setBtn" onclick="togglePanel('setPanel',this)">Settings</button>
    </div>

    <!-- Overview panel -->
    <div class="overview" id="overviewPanel">
      <div class="ov-q" id="ovQuestion"></div>
      <div class="ov-grid" id="ovGrid"></div>
      <div class="ov-stage-desc" id="ovStageDesc"></div>
    </div>

    <!-- Control bar -->
    <div class="ctrl">
      <button class="btn-run" id="btnAuto" onclick="doAuto(this)">Auto</button>
      <button class="btn" id="btnStep" onclick="doStep(this)">Step</button>
      <button class="btn" id="btnReview" onclick="doReview(this)">Review</button>
      <button class="btn-approve" id="btnApprove" onclick="doApprove()" style="display:none">Approve</button>
      <input id="gateFeedback" style="display:none;min-width:120px;flex:1" placeholder="Feedback (optional)">
      <button class="btn-stop" id="btnReject" onclick="doReject()" style="display:none">Reject</button>
      <button class="btn-stop" id="btnStop" onclick="doStop()" style="display:none">Stop</button>
      <span style="margin:0 4px;color:var(--bd)">|</span>
      <label>Until</label>
      <select id="untilStage" style="max-width:140px"><option value="">all stages</option></select>
      <label>Rev</label>
      <select id="maxRev" style="width:50px"><option>1</option><option>2</option><option selected>3</option><option>5</option></select>
      <input id="instrInput" style="flex:1;min-width:80px" placeholder="Instruction (optional)">
      <div style="flex:999"></div>
      <div class="status-pill idle" id="statusPill">
        <span id="statusIcon"></span>
        <span id="statusLabel">Idle</span>
      </div>
    </div>

    <!-- Settings panel -->
    <div class="panel" id="setPanel">
      <h3>CLI Backend Configuration</h3>
      <div class="sgrid" id="setGrid"></div>
      <div style="display:flex;align-items:center;margin-top:10px;gap:10px">
        <button class="btn-run" onclick="saveSettings()">Save</button>
        <span id="saveMsg" style="font-size:11px;color:var(--gr);display:none"></span>
      </div>
    </div>

    <!-- Stats panel -->
    <div class="panel" id="statsPanel">
      <h3>Cost &amp; Duration Analytics</h3>
      <div id="statsContent"></div>
    </div>

    <!-- Stages -->
    <div class="stages" id="stagesBar"></div>

    <!-- Timeline + Detail -->
    <div class="main">
      <div class="side">
        <h3>Timeline (<span id="evCnt">0</span>)</h3>
        <div style="padding:4px 8px;border-bottom:1px solid var(--bd)">
          <select id="tlFilterStage" onchange="renderTimeline()" style="width:100%;font-size:10px;padding:2px 4px">
            <option value="">All stages</option>
          </select>
        </div>
        <div class="side-scroll" id="timeline"></div>
      </div>
      <div class="det">
        <div class="det-hdr">
          <h2 id="detTitle">Select a version</h2>
          <button class="btn" onclick="toggleAll()">Show All</button>
        </div>
        <div class="det-scroll" id="detScroll"></div>
      </div>
    </div>

    <!-- Console -->
    <div class="console">
      <div class="console-hdr">
        <span>Console</span>
        <div style="display:flex;gap:4px;align-items:center">
          <input id="conSearch" oninput="conSearch(this.value)" placeholder="Search..." style="width:120px;padding:1px 6px;font-size:10px;border-radius:3px">
          <button class="btn" style="padding:1px 6px;font-size:10px" onclick="openViewer('Full Console Log',document.getElementById('conBody').innerText)">Full Log</button>
          <button class="btn" style="padding:1px 6px;font-size:10px" onclick="document.getElementById('conBody').innerHTML='';document.getElementById('conSearch').value=''">Clear</button>
        </div>
      </div>
      <div class="con-body" id="conBody"></div>
    </div>
  </div>
</div>

<!-- Modal -->
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)hideModal()">
  <div class="modal">
    <h3>New Research Project</h3>
    <input id="newName" placeholder="Project name">
    <input id="newQ" placeholder="Research question">
    <div class="btns">
      <button class="btn" onclick="hideModal()">Cancel</button>
      <button class="btn-run" onclick="createProject()">Create</button>
    </div>
  </div>
</div>

<!-- Full-screen viewer modal -->
<div class="modal-bg" id="viewerBg" onclick="if(event.target===this)closeViewer()">
  <div class="viewer">
    <div class="viewer-hdr">
      <h3 id="viewerTitle">Output</h3>
      <div style="display:flex;gap:6px;flex-shrink:0">
        <button class="btn" onclick="copyViewer()">Copy</button>
        <button class="btn" onclick="closeViewer()">Close</button>
      </div>
    </div>
    <pre class="viewer-body" id="viewerBody"></pre>
  </div>
</div>

<script>
let DATA=__DATA__,STAGES=__STAGES__,CFG=__CFG__,PROJECTS=__PROJECTS__,PID=__PID__,PIPE=__PIPE__;
const OC_MODELS=__OC__,STAGE_NAMES=__STAGE_NAMES__;
const BACKENDS=['claude','codex','opencode'];
const MODELS={claude:['claude-sonnet-4-20250514','claude-opus-4-20250514','claude-haiku-4-5-20251001'],codex:['gpt-5.4','gpt-5.4-mini','gpt-4.1','gpt-4o','o3'],opencode:OC_MODELS};
const EFFORTS={claude:['max','high','medium','low'],codex:['xhigh','high','medium','low'],opencode:['max','high','medium','low','minimal']};
const ROLES=['researcher','engineer','critic','orchestrator'];
const ICONS={agent_run:'\u25B6',gate_review:'\u25C6',gate_passed:'\u2713',gate_failed:'\u2717',stage_advance:'\u23E9',stage_rollback:'\u21A9',human_approve:'\uD83D\uDC64',human_reject:'\u270B',human_feedback:'\uD83D\uDCAC'};
const ICOLORS={agent_run:'var(--bl)',gate_passed:'var(--gr)',gate_failed:'var(--rd)',stage_advance:'var(--cy)',stage_rollback:'var(--or)',human_approve:'var(--gr)',human_reject:'var(--rd)',human_feedback:'var(--yl)'};

// ============ INIT ============
function init(){
  document.getElementById('projName').textContent=DATA.project_name||'No Project';
  document.getElementById('costLabel').textContent='$'+DATA.total_cost;
  document.getElementById('verLabel').textContent='v'+DATA.current_version;
  const us=document.getElementById('untilStage');
  STAGE_NAMES.forEach(s=>us.innerHTML+=`<option value="${s}">${s.replace(/_/g,' ')}</option>`);
  document.getElementById('evCnt').textContent=DATA.timeline.length;
  renderSidebar();renderOverview();renderStages();renderTimeline();renderStats();buildSettings();updateUI();
}

// ============ SIDEBAR — project list ============
function renderSidebar(){
  const el=document.getElementById('sbList');el.innerHTML='';
  PROJECTS.forEach(p=>{
    const isActive=p.id===PID;
    const isRunning=PIPE.running&&isActive;
    const d=document.createElement('div');
    d.className='sb-item'+(isActive?' active':'');
    d.onclick=()=>{if(!isActive){switchProject(p.id)}};
    d.innerHTML=`
      ${isRunning?'<div class="spinner"></div>':''}
      <div class="pi-info">
        <div class="pi-name">${p.name}</div>
        <div class="pi-stage">${p.stage.replace(/_/g,' ')}</div>
      </div>
      <button class="pi-del" onclick="event.stopPropagation();delProject('${p.id}')" title="Delete">&times;</button>`;
    el.appendChild(d);
  });
}

// ============ OVERVIEW ============
function renderOverview(){
  const q=DATA.research_question;
  document.getElementById('ovQuestion').textContent=q||'No research question set';
  const cs=DATA.current_stage||'';
  const si=DATA.stage_info||{};
  const info=si[cs]||{};
  const stg=DATA.stages||[];
  const cur=stg.find(s=>s.status==='active')||{};
  // Stats grid
  const cards=[
    {l:'Stage',v:(cs||'–').replace(/_/g,' '),sm:true},
    {l:'Version',v:'v'+DATA.current_version},
    {l:'Cost',v:'$'+DATA.total_cost},
    {l:'Duration',v:fmtDur(DATA.total_duration||0)},
    {l:'Artifacts',v:DATA.artifact_count||0},
    {l:'Iterations',v:cur.iteration||0},
  ];
  document.getElementById('ovGrid').innerHTML=cards.map(c=>`<div class="ov-card"><div class="ov-label">${c.l}</div><div class="ov-val${c.sm?' sm':''}">${c.v}</div></div>`).join('');
  // Stage description
  const desc=cur.description||'';
  const gv=cur.gate_verdict;
  const gvHtml=gv?` &mdash; Gate: <span class="vd vd-${gv}">${gv}</span>`:'';
  document.getElementById('ovStageDesc').innerHTML=desc?`<b>${(cs||'').replace(/_/g,' ')}</b>: ${desc}${gvHtml}`:'';
}
function fmtDur(s){if(s<60)return s.toFixed(0)+'s';if(s<3600)return (s/60).toFixed(1)+'m';return (s/3600).toFixed(1)+'h'}

// ============ STAGES ============
function renderStages(){
  const el=document.getElementById('stagesBar');el.innerHTML='';
  STAGES.forEach(s=>{
    const bk=(CFG.agents||{})[s.agent]||{};
    const si=(DATA.stage_info||{})[s.name]||{};
    const d=document.createElement('div');d.className='schip '+s.status;
    const iter=s.iteration||0;
    const gv=s.gate_verdict;
    const gvTag=gv?`<span style="font-size:9px;font-weight:600;color:${gv==='PASS'?'var(--gr)':gv==='FAIL'?'var(--rd)':'var(--yl)'}">${gv}</span>`:'';
    // Show stage description inline (not just tooltip)
    const desc=s.description||'';
    const descHtml=desc?`<div style="font-size:10px;line-height:1.3;margin-top:3px;opacity:0.85;white-space:normal">${desc}</div>`:'';
    d.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center"><span>v${s.index}.x ${s.name.replace(/_/g,' ')}${iter>1?` <span class="iter">${iter}</span>`:''}</span>${gvTag}</div>${descHtml}<span class="al">${s.agent} (${bk.backend||'claude'})${si.cost_usd?' · $'+si.cost_usd:''}</span>`;
    el.appendChild(d);
  });
}

// ============ TIMELINE ============
let versions={},sortedV=[],_lastAutoSelVer='';
function renderTimeline(){
  versions={};
  DATA.timeline.forEach(e=>{if(!versions[e.version])versions[e.version]={events:[],stage:e.stage};versions[e.version].events.push(e)});
  sortedV=Object.keys(versions).sort((a,b)=>{const[ma,ia]=a.split('.').map(Number),[mb,ib]=b.split('.').map(Number);return ma!==mb?ma-mb:ia-ib});

  // Populate stage filter dropdown (preserve selection)
  const fs=document.getElementById('tlFilterStage'),fv=fs.value;
  const stagesInTl=[...new Set(Object.values(versions).map(v=>v.stage))];
  fs.innerHTML='<option value="">All stages</option>'+stagesInTl.map(s=>`<option value="${s}"${s===fv?' selected':''}>${s.replace(/_/g,' ')}</option>`).join('');

  // Apply filter
  const filter=fs.value;
  const filtered=filter?sortedV.filter(v=>versions[v].stage===filter):sortedV;

  const el=document.getElementById('timeline');el.innerHTML='';
  if(!filtered.length){el.innerHTML='<div style="padding:16px;text-align:center;color:var(--dim);font-size:11px">'+(DATA.timeline.length?'No events match filter':'No events yet. Start the pipeline to see activity here.')+'</div>';return}
  filtered.forEach(v=>{
    const g=versions[v],hp=g.events.some(e=>e.event_type==='gate_passed'),hf=g.events.some(e=>e.event_type==='gate_failed');
    const d=document.createElement('div');d.className='vg';
    d.innerHTML=`<div class="vh" data-v="${v}" onclick="selVer('${v}',this)"><span>v${v} ${hp?'\u2713':hf?'\u2717':''}</span><span class="stag">${g.stage.replace(/_/g,' ')}</span></div><div class="ves">${g.events.map(e=>`<div class="ve"><span class="ico" style="color:${ICOLORS[e.event_type]||'var(--dim)'}">${ICONS[e.event_type]||'\u00B7'}</span><span>${e.summary.substring(0,40)}</span></div>`).join('')}</div>`;
    el.appendChild(d);
  });
  // Auto-select latest version ONLY when new events arrive, not on every poll
  if(filtered.length){const l=filtered[filtered.length-1];if(l!==_lastAutoSelVer){_lastAutoSelVer=l;const e=document.querySelector(`.vh[data-v="${l}"]`);if(e)selVer(l,e)}}
}

let dtid=0;
function selVer(v,el){
  document.querySelectorAll('.vh').forEach(h=>h.classList.remove('sel'));
  if(el)el.classList.add('sel');
  const evs=versions[v]?.events||[];
  document.getElementById('detTitle').textContent=`v${v} \u2014 ${(evs[0]?.stage||'').replace(/_/g,' ')}`;
  const sc=document.getElementById('detScroll');sc.innerHTML='';
  evs.forEach(ev=>{
    const c=document.createElement('div');c.className='ec';
    const ac=ev.agent?'ab-'+ev.agent:'',vc=ev.gate_verdict?'vd-'+ev.gate_verdict:'';
    const cs=ev.cost_usd>0?` \u00B7 $${ev.cost_usd.toFixed(3)}`:'',ds=ev.duration_seconds>0?` \u00B7 ${ev.duration_seconds.toFixed(1)}s`:'';
    let h=`<div class="ec-hdr"><div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap">${ev.agent?`<span class="ab ${ac}">${ev.agent}</span>`:''}<span class="ev-s">${ev.summary}</span>${ev.gate_verdict?`<span class="vd ${vc}">${ev.gate_verdict}</span>`:''}</div><span class="ev-m">${ev.timestamp.substring(11,19)}${cs}${ds}</span></div>`;
    if(ev.scores&&Object.keys(ev.scores).length)h+='<div class="scores">'+Object.entries(ev.scores).map(([k,v])=>`<span class="sc ${v>=0.7?'p':'f'}">${k}:${v}</span>`).join('')+'</div>';
    const arts=[...(ev.artifacts_produced||[]),...(ev.artifacts_reviewed||[])];
    if(arts.length)h+='<ul class="al-list">'+arts.map(a=>`<li style="cursor:pointer" onclick="viewArtifact('${a.replace(/'/g,"\\'")}')">${a.split('/').pop()}</li>`).join('')+'</ul>';
    if(ev.detail){const tid='d'+(++dtid);const esc=ev.detail.replace(/&/g,'&amp;').replace(/</g,'&lt;');const il=ev.detail.length>300;
      h+=`<div class="dtxt${il?'':' exp'}" id="${tid}">${esc}</div>`;
      h+=`<div style="display:flex;gap:6px;margin-top:3px">${il?`<span class="dtog" onclick="document.getElementById('${tid}').classList.toggle('exp')">expand/collapse</span>`:''}`;
      h+=`<button class="btn-view" onclick="openViewer('${ev.summary.replace(/'/g,"\\'")}',document.getElementById('${tid}').textContent)">View Full \u2197</button></div>`}
    c.innerHTML=h;sc.appendChild(c);
  });
}
let allExp=false;
function toggleAll(){allExp=!allExp;document.querySelectorAll('.dtxt').forEach(e=>{allExp?e.classList.add('exp'):e.classList.remove('exp')})}

// ============ SETTINGS ============
function buildSettings(){
  const g=document.getElementById('setGrid');g.innerHTML='';
  ROLES.forEach(r=>{
    const c=(CFG.agents||{})[r]||{};
    const d=document.createElement('div');d.className='acard';
    d.innerHTML=`<h4><span class="dot dot-${r}"></span>${r}</h4>
      <div class="crow"><label>CLI</label><select id="s-${r}-b" onchange="onBk('${r}')">${BACKENDS.map(b=>`<option value="${b}" ${c.backend===b?'selected':''}>${b}</option>`).join('')}</select></div>
      <div class="crow"><label>Model</label><select id="s-${r}-m"></select></div>
      <div class="crow"><label>Effort</label><select id="s-${r}-e"></select></div>`;
    g.appendChild(d);onBk(r,c.model,c.effort);
  });
}
function onBk(r,cm,ce){
  const b=document.getElementById(`s-${r}-b`).value;
  const ms=document.getElementById(`s-${r}-m`),es=document.getElementById(`s-${r}-e`);
  const ml=MODELS[b]||[];ms.innerHTML=ml.map(m=>`<option value="${m}">${m}</option>`).join('');if(cm&&ml.includes(cm))ms.value=cm;
  const el=EFFORTS[b]||['high'];es.innerHTML=el.map(e=>`<option value="${e}">${e}</option>`).join('');if(ce&&el.includes(ce))es.value=ce;
}
function togglePanel(id,btn){const p=document.getElementById(id);const v=p.classList.toggle('vis');if(btn)btn.classList.toggle('active',v)}
function saveSettings(){
  const s={};ROLES.forEach(r=>s[r]={backend:document.getElementById(`s-${r}-b`).value,model:document.getElementById(`s-${r}-m`).value,effort:document.getElementById(`s-${r}-e`).value});
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s)}).then(r=>r.json()).then(d=>{
    const m=document.getElementById('saveMsg');m.style.display='inline';m.textContent=d.ok?'Saved! Next step will use new settings.':'Error: '+d.error;
    setTimeout(()=>m.style.display='none',3000);
    if(d.ok){CFG.agents=CFG.agents||{};ROLES.forEach(r=>{CFG.agents[r]={...CFG.agents[r],...s[r]}});renderStages()}
  });
}

// ============ PIPELINE CONTROL ============
function doAuto(b){postAPI('/api/pipeline/auto',{until:document.getElementById('untilStage').value,max_rev:parseInt(document.getElementById('maxRev').value),instruction:document.getElementById('instrInput').value},b)}
function doStep(b){postAPI('/api/pipeline/step',{instruction:document.getElementById('instrInput').value},b)}
function doReview(b){postAPI('/api/pipeline/review',{},b)}
function doApprove(){const fb=document.getElementById('gateFeedback').value;postAPI('/api/pipeline/approve',{feedback:fb});document.getElementById('gateFeedback').value=''}
function doReject(){const fb=document.getElementById('gateFeedback').value;if(!fb){appendCon('Please provide feedback when rejecting.',true);return}postAPI('/api/pipeline/reject',{feedback:fb});document.getElementById('gateFeedback').value=''}
function doStop(){postAPI('/api/pipeline/stop',{})}
function postAPI(url,data,btn){
  if(btn){btn.disabled=true;btn._origText=btn.textContent;btn.textContent='...'}
  fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(r=>r.json()).then(d=>{
    if(d.error)appendCon('ERROR: '+d.error,true);
    setTimeout(pollStatus,300);
  }).finally(()=>{if(btn){btn.disabled=false;btn.textContent=btn._origText}});
}

// ============ POLLING ============
let lastLogLen=0,lastRunning=false,pollTimer=null;
function pollStatus(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    PIPE=d;updateUI();
    if(d.log&&d.log.length>lastLogLen){
      d.log.slice(lastLogLen).forEach(l=>appendCon(`[${l.t}] ${l.m}`,l.m.includes('ERROR')));
      lastLogLen=d.log.length;
    }
    if(d.running||lastRunning){
      fetch('/api/state').then(r=>r.json()).then(sd=>{
        DATA=sd;
        document.getElementById('verLabel').textContent='v'+sd.current_version;
        document.getElementById('costLabel').textContent='$'+sd.total_cost;
        document.getElementById('evCnt').textContent=sd.timeline.length;
        renderOverview();renderTimeline();renderStages();renderStats();
      });
    }
    checkNotif();
    lastRunning=d.running;
    // Adaptive poll rate
    clearInterval(pollTimer);
    pollTimer=setInterval(pollStatus,d.running?2000:8000);
  }).catch(()=>{
    const pill=document.getElementById('statusPill');
    pill.className='status-pill idle';
    document.getElementById('statusLabel').textContent='Connection lost';
  });
}

function updateUI(){
  const r=PIPE.running,w=PIPE.waiting_approval;
  // Status pill
  const pill=document.getElementById('statusPill');
  pill.className='status-pill '+(r?(w?'waiting':'running'):'idle');
  document.getElementById('statusIcon').innerHTML=r?'<div class="spinner" style="width:10px;height:10px;border-width:1.5px"></div>':'';
  document.getElementById('statusLabel').textContent=r?(w?'Awaiting Approval':`${PIPE.mode}: ${PIPE.stage}`):'Idle';
  // Buttons
  document.getElementById('btnAuto').disabled=r;
  document.getElementById('btnStep').disabled=r;
  document.getElementById('btnReview').disabled=r;
  document.getElementById('btnApprove').style.display=w?'inline-block':'none';
  document.getElementById('gateFeedback').style.display=w?'inline-block':'none';
  document.getElementById('btnReject').style.display=w?'inline-block':'none';
  document.getElementById('btnStop').style.display=r&&!w?'inline-block':'none';
  // Sidebar running indicator
  renderSidebar();
}

function appendCon(msg,isErr){
  const el=document.getElementById('conBody');
  const d=document.createElement('div');if(isErr)d.className='err';
  d.textContent=msg;el.appendChild(d);el.scrollTop=el.scrollHeight;
}

// ============ FULL-SCREEN VIEWER ============
function openViewer(title,content){
  document.getElementById('viewerTitle').textContent=title;
  document.getElementById('viewerBody').textContent=content;
  document.getElementById('viewerBg').classList.add('vis');
}
function closeViewer(){document.getElementById('viewerBg').classList.remove('vis')}
function copyViewer(){
  const t=document.getElementById('viewerBody').textContent;
  navigator.clipboard.writeText(t).then(()=>{
    const b=document.querySelector('#viewerBg .btn');if(b){b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',1500)}
  });
}
function viewArtifact(path){
  fetch('/api/artifact?path='+encodeURIComponent(path)).then(r=>r.json()).then(d=>{
    if(d.ok)openViewer(path.split('/').pop(),d.content);
    else appendCon('Error loading artifact: '+(d.error||'unknown'),true);
  });
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeViewer()});

// ============ PROJECT MANAGEMENT ============
function switchProject(pid){
  fetch('/api/project/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:pid})}).then(()=>
    Promise.all([fetch('/api/state').then(r=>r.json()),fetch('/api/status').then(r=>r.json()),fetch('/api/projects').then(r=>r.json()),fetch('/api/config').then(r=>r.json())])
  ).then(([sd,st,prj,cfg])=>{
    DATA=sd;PIPE=st;PROJECTS=prj;CFG=cfg;PID=pid;lastLogLen=0;
    document.getElementById('conBody').innerHTML='';
    STAGES=sd.stages||[];
    init();
  });
}
function delProject(pid){
  if(pid===PID){appendCon('Cannot delete active project',true);return}
  if(!confirm('Delete this project?'))return;
  fetch('/api/project/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:pid})}).then(r=>r.json()).then(d=>{
    if(d.ok){PROJECTS=PROJECTS.filter(p=>p.id!==pid);renderSidebar()}
  });
}
function showModal(){document.getElementById('modalBg').classList.add('vis');document.getElementById('newName').focus()}
function hideModal(){document.getElementById('modalBg').classList.remove('vis')}
function createProject(){
  const n=document.getElementById('newName').value.trim(),q=document.getElementById('newQ').value.trim();
  if(!n||!q)return;
  fetch('/api/project/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,question:q})}).then(r=>r.json()).then(d=>{if(d.ok)location.reload()});
}

// ============ BROWSER NOTIFICATIONS ============
let lastWaiting=false,notifEnabled=false;
function enableNotif(){
  if(!('Notification' in window))return;
  Notification.requestPermission().then(p=>{notifEnabled=p==='granted'});
}
function checkNotif(){
  if(!notifEnabled)return;
  if(PIPE.waiting_approval&&!lastWaiting)new Notification('Approval Needed',{body:'Human gate reached. Review and approve.'});
  if(lastRunning&&!PIPE.running)new Notification('Pipeline Finished',{body:'Research pipeline has stopped.'});
  lastWaiting=PIPE.waiting_approval;
}

// ============ KEYBOARD SHORTCUTS ============
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA'||e.target.tagName==='SELECT')return;
  if(e.ctrlKey&&e.key==='Enter'){e.preventDefault();if(!PIPE.running)doAuto()}
  if(e.ctrlKey&&e.key==='.'){e.preventDefault();doStop()}
});

// ============ STATS PANEL ============
function renderStats(){
  const si=DATA.stage_info||{};const el=document.getElementById('statsContent');
  // Cost per stage
  const costEntries=STAGE_NAMES.map(s=>({name:s.replace(/_/g,' '),val:(si[s]||{}).cost_usd||0}));
  const maxCost=Math.max(...costEntries.map(e=>e.val),0.001);
  // Duration per stage
  const durEntries=STAGE_NAMES.map(s=>({name:s.replace(/_/g,' '),val:(si[s]||{}).duration_s||0}));
  const maxDur=Math.max(...durEntries.map(e=>e.val),1);
  // Per-agent cost
  const agentCost={};(DATA.timeline||[]).forEach(e=>{if(e.agent&&e.cost_usd)agentCost[e.agent]=(agentCost[e.agent]||0)+e.cost_usd});
  const maxAC=Math.max(...Object.values(agentCost),0.001);

  let h='<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">';
  h+='<div><h4 style="font-size:11px;color:var(--dim);margin-bottom:8px">Cost per Stage</h4>';
  costEntries.forEach(e=>{h+=`<div class="stat-row"><span class="stat-lbl">${e.name}</span><div class="stat-bar"><div class="stat-fill" style="width:${(e.val/maxCost*100).toFixed(1)}%;background:var(--bl)"></div></div><span class="stat-val">$${e.val.toFixed(3)}</span></div>`});
  h+='</div><div><h4 style="font-size:11px;color:var(--dim);margin-bottom:8px">Duration per Stage</h4>';
  durEntries.forEach(e=>{h+=`<div class="stat-row"><span class="stat-lbl">${e.name}</span><div class="stat-bar"><div class="stat-fill" style="width:${(e.val/maxDur*100).toFixed(1)}%;background:var(--gr)"></div></div><span class="stat-val">${fmtDur(e.val)}</span></div>`});
  h+='</div></div>';
  if(Object.keys(agentCost).length){
    h+='<h4 style="font-size:11px;color:var(--dim);margin:12px 0 8px">Cost per Agent</h4>';
    Object.entries(agentCost).sort((a,b)=>b[1]-a[1]).forEach(([a,v])=>{h+=`<div class="stat-row"><span class="stat-lbl">${a}</span><div class="stat-bar"><div class="stat-fill" style="width:${(v/maxAC*100).toFixed(1)}%;background:var(--pu)"></div></div><span class="stat-val">$${v.toFixed(3)}</span></div>`});
  }
  el.innerHTML=h;
}

// ============ CONSOLE SEARCH ============
function conSearch(q){
  const lines=document.getElementById('conBody').children;
  for(const l of lines){
    if(!q){l.style.display='';l.innerHTML=l.textContent;continue}
    const t=l.textContent;
    if(t.toLowerCase().includes(q.toLowerCase())){l.style.display='';const re=new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','gi');l.innerHTML=t.replace(re,'<mark style="background:var(--yl);color:var(--bg);padding:0 1px;border-radius:2px">$1</mark>')}
    else l.style.display='none';
  }
}

// Start
enableNotif();
init();
pollTimer=setInterval(pollStatus,PIPE.running?2000:8000);
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_stage_descriptions(base_dir: Path) -> dict[str, str]:
    """Load stage descriptions from stages.yaml."""
    p = base_dir / "config" / "stages.yaml"
    if not p.exists():
        return {}
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
        return {k: v.get("description", "") for k, v in cfg.get("stages", {}).items()}
    except Exception:
        return {}


def build_gui_data(state: ProjectState, base_dir: Path | None = None) -> dict:
    ci = STAGE_ORDER.index(state.current_stage)

    # Load stage descriptions
    stage_descs = _load_stage_descriptions(base_dir) if base_dir else {}

    stages = []
    for i, s in enumerate(STAGE_ORDER):
        gates = [g for g in state.gate_results if g.stage == s]
        if i < ci:
            st = "done"
        elif i == ci:
            st = "failed" if gates and gates[-1].status.value == "failed" else "active"
        else:
            st = ""
        # Latest gate verdict for this stage
        latest_verdict = ""
        if gates:
            for ev in reversed(state.timeline):
                if ev.stage == s and ev.gate_verdict:
                    latest_verdict = ev.gate_verdict
                    break
        stages.append({
            "index": i, "name": s.value, "agent": STAGE_PRIMARY_AGENT[s].value,
            "status": st, "description": stage_descs.get(s.value, ""),
            "iteration": state.iteration_count.get(s.value, 0),
            "gate_verdict": latest_verdict,
        })

    # Per-stage cost and duration from timeline
    stage_info = {}
    for s in STAGE_ORDER:
        cost = sum(e.cost_usd for e in state.timeline if e.stage == s)
        dur = sum(e.duration_seconds for e in state.timeline if e.stage == s)
        art_count = len([a for a in state.artifacts if a.stage == s])
        stage_info[s.value] = {"cost_usd": round(cost, 4), "duration_s": round(dur, 1),
                               "artifact_count": art_count}

    timeline = []
    for ev in state.timeline:
        timeline.append({
            "version": ev.version, "event_type": ev.event_type.value,
            "agent": ev.agent.value if ev.agent else None, "stage": ev.stage.value,
            "summary": ev.summary, "detail": _ANSI_RE.sub('', ev.detail or ''),
            "artifacts_produced": ev.artifacts_produced, "artifacts_reviewed": ev.artifacts_reviewed,
            "gate_verdict": ev.gate_verdict, "scores": ev.scores,
            "cost_usd": ev.cost_usd, "duration_seconds": ev.duration_seconds,
            "timestamp": ev.timestamp.isoformat(),
        })

    # Total duration
    total_dur = sum(e.duration_seconds for e in state.timeline)

    # Artifact summary
    artifact_summary = [
        {"type": a.artifact_type.value, "version": a.version,
         "stage": a.stage.value, "path": a.path}
        for a in state.artifacts
    ]

    return {
        "project_name": state.name, "project_id": state.project_id,
        "research_question": state.research_question or "",
        "current_version": state.current_version(),
        "current_stage": state.current_stage.value,
        "total_cost": f"{state.total_cost():.4f}",
        "total_duration": round(total_dur, 1),
        "artifact_count": len(state.artifacts),
        "stage_info": stage_info,
        "artifact_summary": artifact_summary,
        "timeline": timeline, "stages": stages,
    }


def render_html(state: ProjectState, config: dict, projects: list, pipe_status: dict,
                base_dir: Path | None = None) -> str:
    data = build_gui_data(state, base_dir=base_dir)
    oc_models = _get_opencode_models()
    proj_list = [{"id": p.project_id, "name": p.name, "stage": p.current_stage.value} for p in projects]

    html = _HTML
    replacements = {
        "__DATA__": json.dumps(data),
        "__STAGES__": json.dumps(data["stages"]),
        "__CFG__": json.dumps(config),
        "__PROJECTS__": json.dumps(proj_list),
        "__PID__": json.dumps(state.project_id),
        "__PIPE__": json.dumps(pipe_status),
        "__OC__": json.dumps(oc_models),
        "__STAGE_NAMES__": json.dumps([s.value for s in STAGE_ORDER]),
    }
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

def run_gui(sm: StateManager, project_id: Optional[str], config: dict, port: int = 8080):
    from http.server import HTTPServer, BaseHTTPRequestHandler

    base_dir = sm.base_dir
    config_path = base_dir / "config" / "settings.yaml"
    runner = PipelineRunner(sm, base_dir, config)
    last_cfg_mtime = config_path.stat().st_mtime if config_path.exists() else 0

    class H(BaseHTTPRequestHandler):
        def _json_ok(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def _reload_cfg(self):
            """Only reload config if the yaml file was modified since last check."""
            nonlocal config, last_cfg_mtime
            if not config_path.exists():
                return
            mtime = config_path.stat().st_mtime
            if mtime <= last_cfg_mtime:
                return  # File unchanged, skip reload
            last_cfg_mtime = mtime
            config = yaml.safe_load(config_path.read_text()) or {}
            runner.reload_config(config, silent=True)  # Silent — no log spam

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._reload_cfg()
                pid = runner.project_id or project_id
                try:
                    state = sm.load_project(pid) if pid else None
                except Exception:
                    state = None
                if state is None:
                    state = ProjectState(project_id="none", name="No Project", research_question="")
                projects = sm.list_projects()
                html = render_html(state, config, projects, runner.get_status(), base_dir=base_dir)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())

            elif self.path == "/api/state":
                pid = runner.project_id or project_id
                try:
                    state = sm.load_project(pid) if pid else None
                except Exception:
                    state = None
                if state:
                    self._json_ok(build_gui_data(state, base_dir=base_dir))
                else:
                    self._json_ok({"timeline": [], "stages": [], "current_version": "0.0", "total_cost": "0"})

            elif self.path == "/api/status":
                self._json_ok(runner.get_status())

            elif self.path == "/api/projects":
                ps = sm.list_projects()
                self._json_ok([{"id": p.project_id, "name": p.name, "stage": p.current_stage.value} for p in ps])

            elif self.path == "/api/config":
                self._reload_cfg()
                self._json_ok(config)

            elif self.path.startswith("/api/artifact"):
                import urllib.parse
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                rel_path = params.get("path", [""])[0]
                pid = runner.project_id or project_id
                if pid and rel_path:
                    # Handle both relative and projects/-prefixed paths
                    if rel_path.startswith("projects/"):
                        full = base_dir / rel_path
                    else:
                        full = base_dir / "projects" / pid / rel_path
                    if full.exists() and full.stat().st_size < 500_000:
                        try:
                            content = full.read_text(encoding="utf-8")
                            self._json_ok({"ok": True, "content": content, "path": rel_path})
                        except Exception as e:
                            self._json_ok({"ok": False, "error": str(e)})
                    else:
                        self._json_ok({"ok": False, "error": "Not found or too large"})
                else:
                    self._json_ok({"ok": False, "error": "Missing path or project"})

            else:
                self.send_error(404)

        def do_POST(self):
            body = self._read_body()

            if self.path == "/api/config":
                try:
                    if "agents" not in config:
                        config["agents"] = {}
                    for role, vals in body.items():
                        if role not in config["agents"]:
                            config["agents"][role] = {}
                        for k in ("backend", "model", "effort"):
                            if k in vals:
                                config["agents"][role][k] = vals[k]
                    if config_path.exists() or True:
                        config_path.parent.mkdir(parents=True, exist_ok=True)
                        config_path.write_text(
                            yaml.dump(config, default_flow_style=False, allow_unicode=True, width=120),
                            encoding="utf-8")
                    nonlocal last_cfg_mtime
                    last_cfg_mtime = config_path.stat().st_mtime
                    runner.reload_config(config, silent=False)  # User clicked Save → log it
                    self._json_ok({"ok": True})
                except Exception as e:
                    self._json_ok({"ok": False, "error": str(e)})

            elif self.path == "/api/project/create":
                try:
                    pid = runner.create_project(body["name"], body["question"])
                    self._json_ok({"ok": True, "id": pid})
                except Exception as e:
                    self._json_ok({"ok": False, "error": str(e)})

            elif self.path == "/api/project/switch":
                try:
                    runner.project_id = body["id"]
                    self._json_ok({"ok": True})
                except Exception as e:
                    self._json_ok({"ok": False, "error": str(e)})

            elif self.path == "/api/project/delete":
                try:
                    did = body["id"]
                    if did == (runner.project_id or project_id):
                        self._json_ok({"ok": False, "error": "Cannot delete active project"})
                    else:
                        sm.delete_project(did)
                        self._json_ok({"ok": True})
                except Exception as e:
                    self._json_ok({"ok": False, "error": str(e)})

            elif self.path == "/api/pipeline/auto":
                if runner.running:
                    self._json_ok({"ok": False, "error": "Pipeline already running"})
                else:
                    runner.start_auto(body.get("until", ""), body.get("max_rev", 3), body.get("instruction", ""))
                    self._json_ok({"ok": True})

            elif self.path == "/api/pipeline/step":
                if runner.running:
                    self._json_ok({"ok": False, "error": "Pipeline already running"})
                else:
                    runner.start_step(body.get("instruction", ""))
                    self._json_ok({"ok": True})

            elif self.path == "/api/pipeline/review":
                if runner.running:
                    self._json_ok({"ok": False, "error": "Pipeline already running"})
                else:
                    runner.start_review()
                    self._json_ok({"ok": True})

            elif self.path == "/api/pipeline/approve":
                runner.approve(body.get("feedback", ""))
                self._json_ok({"ok": True})

            elif self.path == "/api/pipeline/reject":
                runner.reject(body.get("feedback", ""))
                self._json_ok({"ok": True})

            elif self.path == "/api/pipeline/stop":
                runner.stop()
                self._json_ok({"ok": True})

            else:
                self.send_error(404)

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    host = config.get("gui", {}).get("host", "127.0.0.1")
    server = HTTPServer((host, port), H)
    print(f"Research Agent GUI: http://{host}:{port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGUI stopped.")
        server.server_close()
