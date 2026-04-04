"""Gate evaluation system — the quality checkpoint between stages.

Gates are the key mechanism that replaces subjective "I think it's ready" with
machine-verifiable checks. Each gate has three types of checks:

1. Schema checks (automated): does the artifact conform to its YAML schema?
2. Codex review: does the Codex Critic (via codex-plugin-cc) approve the content?
3. Human checks (optional): does a human approve at configured checkpoints?

A gate passes only when ALL blocking checks pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .agents.critic import CriticAgent
from .artifacts import assemble_context, load_schema, validate_artifact_content
from .models import (
    STAGE_REQUIRED_ARTIFACTS,
    STAGE_REVIEWER,
    AgentRole,
    ArtifactType,
    AutomationLevel,
    GateCheck,
    GateResult,
    GateStatus,
    ProjectState,
    Stage,
)
from .state import StateManager


class GateEvaluator:
    """Evaluates whether a project can advance past a stage's gate."""

    def __init__(
        self,
        state_manager: StateManager,
        schema_dir: Path,
        automation_level: AutomationLevel = AutomationLevel.HYBRID,
        human_gates: Optional[list[Stage]] = None,
        max_iterations: int = 5,
        codex_model: str = "gpt-5.4",
        codex_effort: str = "xhigh",
        project_dir: Optional[Path] = None,
    ):
        self.state_mgr = state_manager
        self.schema_dir = schema_dir
        self.automation_level = automation_level
        self.human_gates = human_gates or [
            Stage.HYPOTHESIS_FORMATION,
            Stage.EXPERIMENTATION,
        ]
        self.max_iterations = max_iterations
        self.critic = CriticAgent(
            model=codex_model,
            effort=codex_effort,
            project_dir=project_dir,
        )

    def evaluate(
        self,
        state: ProjectState,
        stage: Stage,
    ) -> GateResult:
        """Run all gate checks for a stage. Returns a GateResult."""
        checks: list[GateCheck] = []
        iteration = state.iteration_count.get(stage.value, 1)

        # --- Check 1: Required artifacts exist ---
        checks.append(self._check_artifacts_exist(state, stage))

        # --- Check 2: Schema validation for each required artifact ---
        for artifact_type in STAGE_REQUIRED_ARTIFACTS.get(stage, []):
            check = self._check_schema(state, stage, artifact_type)
            if check:
                checks.append(check)

        # --- Check 3: Iteration limit ---
        if iteration > self.max_iterations:
            checks.append(GateCheck(
                name="iteration_limit",
                description=f"Stage attempted {iteration} times (max: {self.max_iterations})",
                check_type="automated",
                passed=False,
                feedback=(
                    f"This stage has been iterated {iteration} times without passing. "
                    "Consider pivoting or escalating to human review."
                ),
            ))

        # --- Check 4: Codex review ---
        # Only run Codex if basic checks pass (avoid wasting Codex budget on broken artifacts)
        basic_pass = all(c.passed for c in checks)
        if basic_pass:
            codex_check = self._check_codex_review(state, stage)
            checks.append(codex_check)

        # --- Determine overall status ---
        all_passed = all(c.passed for c in checks)
        needs_human = self._needs_human_approval(stage)

        if all_passed and needs_human:
            status = GateStatus.HUMAN_REVIEW
        elif all_passed:
            status = GateStatus.PASSED
        else:
            status = GateStatus.FAILED

        # Build feedback summary
        failed_checks = [c for c in checks if not c.passed]
        if failed_checks:
            feedback_parts = ["Gate FAILED. Issues found:"]
            for c in failed_checks:
                feedback_parts.append(f"  - [{c.name}] {c.feedback}")
        else:
            feedback_parts = ["Gate PASSED. All checks satisfied."]

        result = GateResult(
            gate_name=f"{stage.value}_gate",
            stage=stage,
            status=status,
            checks=checks,
            reviewer=STAGE_REVIEWER.get(stage),
            overall_feedback="\n".join(feedback_parts),
            iteration=iteration,
        )
        state.gate_results.append(result)
        return result

    # -----------------------------------------------------------------------
    # Individual checks
    # -----------------------------------------------------------------------

    def _check_artifacts_exist(self, state: ProjectState, stage: Stage) -> GateCheck:
        required = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
        missing = []
        for atype in required:
            if state.latest_artifact(atype) is None:
                missing.append(atype.value)

        if missing:
            return GateCheck(
                name="artifacts_exist",
                description="Check that all required artifacts are present",
                check_type="automated",
                passed=False,
                feedback=f"Missing required artifacts: {', '.join(missing)}",
            )
        return GateCheck(
            name="artifacts_exist",
            description="Check that all required artifacts are present",
            check_type="automated",
            passed=True,
            feedback="All required artifacts present.",
        )

    def _check_schema(self, state: ProjectState, stage: Stage,
                      artifact_type: ArtifactType) -> Optional[GateCheck]:
        artifact = state.latest_artifact(artifact_type)
        if not artifact:
            return None

        schema = load_schema(self.schema_dir, artifact_type)
        if not schema:
            return GateCheck(
                name=f"schema_{artifact_type.value}",
                description=f"Schema validation for {artifact_type.value}",
                check_type="schema",
                passed=True,
                score=1.0,
                feedback="No schema defined — skipping validation.",
            )

        try:
            content = self.state_mgr.read_artifact_file(state.project_id, artifact)
        except FileNotFoundError:
            return GateCheck(
                name=f"schema_{artifact_type.value}",
                description=f"Schema validation for {artifact_type.value}",
                check_type="schema",
                passed=False,
                feedback=f"Artifact file not found: {artifact.path}",
            )

        errors = validate_artifact_content(content, schema)
        if errors:
            return GateCheck(
                name=f"schema_{artifact_type.value}",
                description=f"Schema validation for {artifact_type.value}",
                check_type="schema",
                passed=False,
                feedback="Schema errors: " + "; ".join(errors),
            )
        return GateCheck(
            name=f"schema_{artifact_type.value}",
            description=f"Schema validation for {artifact_type.value}",
            check_type="schema",
            passed=True,
            score=1.0,
            feedback="Schema validation passed.",
        )

    def _check_codex_review(self, state: ProjectState, stage: Stage) -> GateCheck:
        """Use Codex (via codex-plugin-cc) to review the stage's artifacts."""
        # Assemble context with all relevant artifacts
        required_types = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
        artifact_contents = self.state_mgr.get_latest_artifacts(state, required_types)
        artifact_text = assemble_context(state, artifact_contents, stage)

        project_context = (
            f"Project: {state.name}\n"
            f"Research Question: {state.research_question}\n"
            f"Stage: {stage.value}\n"
            f"Iteration: {state.iteration_count.get(stage.value, 1)}"
        )

        try:
            result, gate_check = self.critic.review(
                stage=stage,
                artifact_content=artifact_text,
                project_context=project_context,
                state=state,
            )
            return gate_check
        except Exception as e:
            return GateCheck(
                name="codex_review",
                description="Codex adversarial review",
                check_type="codex",
                passed=False,
                feedback=f"Codex review failed: {e}",
            )

    def _needs_human_approval(self, stage: Stage) -> bool:
        if self.automation_level == AutomationLevel.MANUAL:
            return True
        if self.automation_level == AutomationLevel.FULL:
            return False
        # Hybrid: only at configured gates
        return stage in self.human_gates
