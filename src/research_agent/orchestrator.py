"""Orchestrator — the state machine that drives the research pipeline.

This is the brain of the system. It:
1. Manages project state and stage transitions
2. Dispatches work to the right agent
3. Runs gate evaluations
4. Handles rollbacks and iterations
5. Tracks costs

The orchestrator does NOT do research itself — it only coordinates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from .agents import EngineerAgent, ResearcherAgent
from .agents.base import BaseAgent
from .agents.critic import CriticAgent
from .artifacts import assemble_context, create_artifact
from .gates import GateEvaluator
from .integrations.llm import LLMClient
from .models import (
    ALLOWED_TRANSITIONS,
    STAGE_ORDER,
    STAGE_PRIMARY_AGENT,
    STAGE_REQUIRED_ARTIFACTS,
    AgentRole,
    ArtifactType,
    AutomationLevel,
    GateResult,
    GateStatus,
    LLMProvider,
    ProjectState,
    Stage,
)
from .state import StateManager


class Orchestrator:
    """Main pipeline orchestrator — coordinates agents, gates, and state."""

    def __init__(self, config: dict[str, Any], base_dir: Path):
        self.config = config
        self.base_dir = base_dir
        self.state_mgr = StateManager(base_dir)
        self.llm = LLMClient()

        # Agent registry — Researcher and Engineer use Claude API
        agent_cfg = config.get("agents", {})
        self.agents: dict[AgentRole, BaseAgent] = {
            AgentRole.RESEARCHER: ResearcherAgent(
                llm_client=self.llm,
                provider=LLMProvider(agent_cfg.get("researcher", {}).get("provider", "claude")),
                model=agent_cfg.get("researcher", {}).get("model", "claude-sonnet-4-20250514"),
                temperature=agent_cfg.get("researcher", {}).get("temperature", 0.7),
            ),
            AgentRole.ENGINEER: EngineerAgent(
                llm_client=self.llm,
                provider=LLMProvider(agent_cfg.get("engineer", {}).get("provider", "claude")),
                model=agent_cfg.get("engineer", {}).get("model", "claude-sonnet-4-20250514"),
                temperature=agent_cfg.get("engineer", {}).get("temperature", 0.4),
            ),
        }

        # Critic uses Codex (via codex-plugin-cc), not direct API
        codex_cfg = agent_cfg.get("critic", {})
        self.critic = CriticAgent(
            model=codex_cfg.get("model", "gpt-5.4"),
            effort=codex_cfg.get("effort", "xhigh"),
            project_dir=base_dir,
        )

        # Gate evaluator — uses Codex for AI reviews
        pipeline_cfg = config.get("pipeline", {})
        automation = AutomationLevel(pipeline_cfg.get("automation_level", "hybrid"))
        human_gates = [Stage(s) for s in pipeline_cfg.get("human_gates", [
            "hypothesis_formation", "experimentation",
        ])]
        self.gate_evaluator = GateEvaluator(
            state_manager=self.state_mgr,
            schema_dir=base_dir / "schemas",
            automation_level=automation,
            human_gates=human_gates,
            max_iterations=pipeline_cfg.get("max_iterations", 5),
            codex_model=codex_cfg.get("model", "gpt-5.4"),
            codex_effort=codex_cfg.get("effort", "xhigh"),
            project_dir=base_dir,
        )

    # -----------------------------------------------------------------------
    # Project lifecycle
    # -----------------------------------------------------------------------

    def create_project(self, name: str, description: str = "",
                       research_question: str = "") -> ProjectState:
        return self.state_mgr.create_project(name, description, research_question)

    def load_project(self, project_id: str) -> ProjectState:
        return self.state_mgr.load_project(project_id)

    def list_projects(self) -> list[ProjectState]:
        return self.state_mgr.list_projects()

    # -----------------------------------------------------------------------
    # Stage execution
    # -----------------------------------------------------------------------

    def run_stage(
        self,
        state: ProjectState,
        instruction: str = "",
        agent_override: Optional[AgentRole] = None,
    ) -> tuple[str, ProjectState]:
        """Execute the primary agent for the current stage.

        Returns (agent_output, updated_state).
        """
        stage = state.current_stage
        role = agent_override or STAGE_PRIMARY_AGENT[stage]
        agent = self.agents[role]

        # Assemble context from existing artifacts
        # Include artifacts from current and all previous stages
        stage_idx = STAGE_ORDER.index(stage)
        relevant_types: list[ArtifactType] = []
        for s in STAGE_ORDER[:stage_idx + 1]:
            relevant_types.extend(STAGE_REQUIRED_ARTIFACTS.get(s, []))
        artifact_contents = self.state_mgr.get_latest_artifacts(state, relevant_types)
        context = assemble_context(state, artifact_contents, stage)

        # Default instruction if none given
        if not instruction:
            instruction = self._default_instruction(stage)

        # Execute agent
        output, response = agent.execute(stage, context, instruction, state)

        # Extract and save artifact if agent produces one
        artifact_type = agent.expected_output_type(stage)
        if artifact_type:
            yaml_content = agent.extract_yaml_block(output)
            if yaml_content:
                version = len([
                    a for a in state.artifacts if a.artifact_type == artifact_type
                ]) + 1
                filename = f"{artifact_type.value}_v{version}.yaml"
                self.state_mgr.save_artifact_file(
                    state.project_id, stage, filename, yaml_content
                )
                create_artifact(
                    state, artifact_type, stage, role, filename,
                    metadata={"model": response.model, "cost": response.cost_usd},
                )

        self.state_mgr.save_project(state)
        return output, state

    def run_gate(self, state: ProjectState) -> GateResult:
        """Evaluate the gate for the current stage."""
        result = self.gate_evaluator.evaluate(state, state.current_stage)
        self.state_mgr.save_project(state)
        return result

    def advance(self, state: ProjectState, force: bool = False) -> tuple[bool, str]:
        """Attempt to advance to the next stage.

        Returns (success, message).
        """
        stage = state.current_stage
        stage_idx = STAGE_ORDER.index(stage)

        if stage_idx >= len(STAGE_ORDER) - 1:
            return False, "Already at the final stage (analysis)."

        # Check gate status
        stage_gates = [g for g in state.gate_results if g.stage == stage]
        if not stage_gates and not force:
            return False, "No gate evaluation found. Run `ra gate` first."

        latest_gate = stage_gates[-1] if stage_gates else None

        if not force:
            if latest_gate and latest_gate.status == GateStatus.FAILED:
                return False, (
                    f"Gate FAILED. Fix the issues and re-run.\n"
                    f"Feedback:\n{latest_gate.overall_feedback}"
                )
            if latest_gate and latest_gate.status == GateStatus.HUMAN_REVIEW:
                return False, (
                    "Gate requires human approval. Use `ra advance --approve` to proceed."
                )

        next_stage = STAGE_ORDER[stage_idx + 1]
        trigger = ALLOWED_TRANSITIONS.get((stage, next_stage), "manual_advance")
        state.record_transition(next_stage, trigger, gate_result=latest_gate)
        self.state_mgr.save_project(state)
        return True, f"Advanced to stage: {next_stage.value}"

    def rollback(self, state: ProjectState, target_stage: Stage,
                 reason: str = "") -> tuple[bool, str]:
        """Roll back to an earlier stage."""
        current_idx = STAGE_ORDER.index(state.current_stage)
        target_idx = STAGE_ORDER.index(target_stage)

        if target_idx >= current_idx:
            return False, "Can only roll back to earlier stages."

        transition_key = (state.current_stage, target_stage)
        if transition_key not in ALLOWED_TRANSITIONS:
            return False, (
                f"Transition from {state.current_stage.value} to {target_stage.value} "
                "is not allowed. Check ALLOWED_TRANSITIONS."
            )

        trigger = ALLOWED_TRANSITIONS[transition_key]
        state.record_transition(target_stage, trigger, notes=reason)
        self.state_mgr.save_project(state)
        return True, f"Rolled back to stage: {target_stage.value} (iteration {state.current_iteration()})"

    # -----------------------------------------------------------------------
    # Full pipeline execution
    # -----------------------------------------------------------------------

    def run_until_gate(
        self,
        state: ProjectState,
        instruction: str = "",
        max_revisions: int = 3,
    ) -> tuple[str, GateResult, ProjectState]:
        """Run agent → gate → revise loop until gate passes or max revisions.

        This is the core automation loop. For each iteration:
        1. Run the primary agent
        2. Evaluate the gate
        3. If failed, feed critique back and re-run
        4. If passed, return

        Returns (final_output, gate_result, updated_state).
        """
        last_output = ""
        last_gate = None

        for i in range(max_revisions + 1):
            # Run the stage
            if i > 0 and last_gate:
                # Incorporate gate feedback into instruction
                revision_instruction = (
                    f"REVISION {i}: The previous version was rejected by the reviewer.\n\n"
                    f"REVIEWER FEEDBACK:\n{last_gate.overall_feedback}\n\n"
                    f"ORIGINAL INSTRUCTION: {instruction}\n\n"
                    "Address ALL the reviewer's feedback and resubmit."
                )
                last_output, state = self.run_stage(state, revision_instruction)
            else:
                last_output, state = self.run_stage(state, instruction)

            # Evaluate gate
            last_gate = self.run_gate(state)

            if last_gate.status in (GateStatus.PASSED, GateStatus.HUMAN_REVIEW):
                break

        return last_output, last_gate, state

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def get_status_summary(self, state: ProjectState) -> dict[str, Any]:
        """Get a summary of project status for display."""
        return {
            "project": state.name,
            "id": state.project_id,
            "stage": state.current_stage.value,
            "iteration": state.current_iteration(),
            "artifacts": len(state.artifacts),
            "gate_results": len(state.gate_results),
            "total_cost": f"${state.total_cost():.4f}",
            "transitions": len(state.transitions),
            "latest_gate": (
                state.gate_results[-1].status.value
                if state.gate_results else "none"
            ),
        }

    def _default_instruction(self, stage: Stage) -> str:
        return {
            Stage.PROBLEM_DEFINITION: (
                "Define the research problem based on the research question. "
                "Be specific about the gap and why it matters."
            ),
            Stage.LITERATURE_REVIEW: (
                "Conduct a thorough literature review for the defined problem. "
                "Find key papers, identify gaps, and recommend baselines."
            ),
            Stage.HYPOTHESIS_FORMATION: (
                "Based on the problem definition and literature review, "
                "formulate a testable research hypothesis."
            ),
            Stage.EXPERIMENT_DESIGN: (
                "Design a complete experiment to test the hypothesis. "
                "Include all baselines, ablations, and success criteria."
            ),
            Stage.IMPLEMENTATION: (
                "Implement the experiment according to the specification. "
                "Ensure reproducibility and include tests."
            ),
            Stage.EXPERIMENTATION: (
                "Analyze the experimental setup and prepare for execution. "
                "Verify all components are ready."
            ),
            Stage.ANALYSIS: (
                "Analyze the experimental results. Draw conclusions about "
                "whether the hypothesis is supported."
            ),
        }.get(stage, "Proceed with the current stage.")
