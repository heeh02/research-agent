"""Tests for research_agent.models — enums, data models, state machine transitions."""
from __future__ import annotations

from datetime import datetime

import pytest

from research_agent.models import (
    ALLOWED_TRANSITIONS,
    STAGE_ORDER,
    STAGE_PRIMARY_AGENT,
    STAGE_REQUIRED_ARTIFACTS,
    STAGE_REVIEWER,
    AgentRole,
    Artifact,
    ArtifactType,
    AutomationLevel,
    CLIBackend,
    CostRecord,
    GateCheck,
    GateResult,
    GateStatus,
    LLMProvider,
    ProjectState,
    Stage,
    StageTransition,
    VersionEvent,
    VersionEventType,
)


# ---------------------------------------------------------------------------
# Enum completeness
# ---------------------------------------------------------------------------

class TestStageEnum:
    def test_stage_count(self):
        assert len(Stage) == 7

    def test_stage_order_matches_enum(self):
        assert STAGE_ORDER == list(Stage)

    def test_stage_values(self):
        assert Stage.PROBLEM_DEFINITION.value == "problem_definition"
        assert Stage.ANALYSIS.value == "analysis"

    def test_str_behavior(self):
        assert str(Stage.PROBLEM_DEFINITION) == "Stage.PROBLEM_DEFINITION"
        assert Stage.PROBLEM_DEFINITION == "problem_definition"


class TestAgentRoleEnum:
    def test_all_roles(self):
        assert set(AgentRole) == {
            AgentRole.ORCHESTRATOR,
            AgentRole.RESEARCHER,
            AgentRole.CRITIC,
            AgentRole.RESEARCH_CRITIC,
            AgentRole.CODE_CRITIC,
            AgentRole.ENGINEER,
        }


class TestCLIBackendEnum:
    def test_backends(self):
        assert CLIBackend.CLAUDE.value == "claude"
        assert CLIBackend.CODEX.value == "codex"
        assert CLIBackend.OPENCODE.value == "opencode"


class TestArtifactTypeEnum:
    def test_artifact_count(self):
        assert len(ArtifactType) == 13

    def test_key_artifacts(self):
        assert ArtifactType.PROBLEM_BRIEF.value == "problem_brief"
        assert ArtifactType.REVIEW_REPORT.value == "review_report"


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_forward_transitions_exist(self):
        """Every consecutive stage pair has a forward transition."""
        for i in range(len(STAGE_ORDER) - 1):
            pair = (STAGE_ORDER[i], STAGE_ORDER[i + 1])
            assert pair in ALLOWED_TRANSITIONS, f"Missing forward transition: {pair}"

    def test_no_self_transitions(self):
        for from_s, to_s in ALLOWED_TRANSITIONS:
            assert from_s != to_s

    def test_backward_transitions_exist(self):
        backward = [(f, t) for (f, t) in ALLOWED_TRANSITIONS
                     if STAGE_ORDER.index(t) < STAGE_ORDER.index(f)]
        assert len(backward) > 0

    def test_no_skip_forward(self):
        """Forward transitions only go one step ahead."""
        for from_s, to_s in ALLOWED_TRANSITIONS:
            fi = STAGE_ORDER.index(from_s)
            ti = STAGE_ORDER.index(to_s)
            if ti > fi:
                assert ti == fi + 1, f"Skipping forward: {from_s} -> {to_s}"


class TestStageMappings:
    def test_every_stage_has_primary_agent(self):
        for stage in Stage:
            assert stage in STAGE_PRIMARY_AGENT

    def test_every_stage_has_reviewer(self):
        for stage in Stage:
            assert stage in STAGE_REVIEWER

    def test_every_stage_has_required_artifacts(self):
        for stage in Stage:
            assert stage in STAGE_REQUIRED_ARTIFACTS
            assert len(STAGE_REQUIRED_ARTIFACTS[stage]) > 0


# ---------------------------------------------------------------------------
# GateCheck / GateResult
# ---------------------------------------------------------------------------

class TestGateModels:
    def test_gate_check_creation(self):
        gc = GateCheck(
            name="schema_valid",
            description="Check YAML schema",
            check_type="schema",
            passed=True,
            score=1.0,
            feedback="OK",
        )
        assert gc.passed
        assert gc.score == 1.0

    def test_gate_result_pass_rate(self):
        checks = [
            GateCheck(name="a", description="", check_type="auto", passed=True),
            GateCheck(name="b", description="", check_type="auto", passed=False),
            GateCheck(name="c", description="", check_type="auto", passed=True),
        ]
        gr = GateResult(
            gate_name="test_gate",
            stage=Stage.PROBLEM_DEFINITION,
            status=GateStatus.PENDING,
            checks=checks,
        )
        assert abs(gr.pass_rate - 2 / 3) < 1e-9

    def test_gate_result_empty_checks(self):
        gr = GateResult(
            gate_name="empty",
            stage=Stage.ANALYSIS,
            status=GateStatus.PASSED,
        )
        assert gr.pass_rate == 0.0


# ---------------------------------------------------------------------------
# Artifact model
# ---------------------------------------------------------------------------

class TestArtifactModel:
    def test_artifact_creation(self):
        a = Artifact(
            name="problem_brief_v1",
            artifact_type=ArtifactType.PROBLEM_BRIEF,
            stage=Stage.PROBLEM_DEFINITION,
            version=1,
            path="artifacts/problem_definition/problem_brief_v1.yaml",
            created_by=AgentRole.RESEARCHER,
        )
        assert a.version == 1
        assert a.metadata == {}
        assert a.provenance == {}


# ---------------------------------------------------------------------------
# ProjectState
# ---------------------------------------------------------------------------

class TestProjectState:
    def test_default_state(self):
        ps = ProjectState(project_id="test-1", name="Test")
        assert ps.current_stage == Stage.PROBLEM_DEFINITION
        assert ps.artifacts == []
        assert ps.total_cost() == 0.0

    def test_current_version(self):
        ps = ProjectState(project_id="t", name="T")
        assert ps.current_version() == "0.1"

    def test_current_version_with_iteration(self):
        ps = ProjectState(
            project_id="t", name="T",
            current_stage=Stage.LITERATURE_REVIEW,
            iteration_count={"literature_review": 3},
        )
        assert ps.current_version() == "1.3"

    def test_increment_iteration(self):
        ps = ProjectState(project_id="t", name="T")
        assert ps.current_iteration() == 1
        ps.increment_iteration()
        assert ps.current_iteration() == 2

    def test_latest_artifact(self):
        ps = ProjectState(project_id="t", name="T")
        a1 = Artifact(
            name="pb_v1", artifact_type=ArtifactType.PROBLEM_BRIEF,
            stage=Stage.PROBLEM_DEFINITION, version=1,
            path="a/b.yaml", created_by=AgentRole.RESEARCHER,
        )
        a2 = Artifact(
            name="pb_v2", artifact_type=ArtifactType.PROBLEM_BRIEF,
            stage=Stage.PROBLEM_DEFINITION, version=2,
            path="a/c.yaml", created_by=AgentRole.RESEARCHER,
        )
        ps.artifacts = [a1, a2]
        latest = ps.latest_artifact(ArtifactType.PROBLEM_BRIEF)
        assert latest is not None
        assert latest.version == 2

    def test_latest_artifact_none(self):
        ps = ProjectState(project_id="t", name="T")
        assert ps.latest_artifact(ArtifactType.PROBLEM_BRIEF) is None

    def test_stage_artifacts(self):
        ps = ProjectState(project_id="t", name="T")
        a1 = Artifact(
            name="a", artifact_type=ArtifactType.PROBLEM_BRIEF,
            stage=Stage.PROBLEM_DEFINITION, version=1,
            path="x", created_by=AgentRole.RESEARCHER,
        )
        a2 = Artifact(
            name="b", artifact_type=ArtifactType.LITERATURE_MAP,
            stage=Stage.LITERATURE_REVIEW, version=1,
            path="y", created_by=AgentRole.RESEARCHER,
        )
        ps.artifacts = [a1, a2]
        assert len(ps.stage_artifacts(Stage.PROBLEM_DEFINITION)) == 1
        assert len(ps.stage_artifacts(Stage.LITERATURE_REVIEW)) == 1

    def test_total_cost(self):
        ps = ProjectState(project_id="t", name="T")
        ps.cost_records = [
            CostRecord(
                agent=AgentRole.RESEARCHER, provider=LLMProvider.CLAUDE,
                model="s", input_tokens=100, output_tokens=50,
                cost_usd=0.01, task_description="t", stage=Stage.PROBLEM_DEFINITION,
            ),
            CostRecord(
                agent=AgentRole.ENGINEER, provider=LLMProvider.OPENAI,
                model="g", input_tokens=200, output_tokens=100,
                cost_usd=0.02, task_description="t2", stage=Stage.IMPLEMENTATION,
            ),
        ]
        assert abs(ps.total_cost() - 0.03) < 1e-9

    def test_stage_cost(self):
        ps = ProjectState(project_id="t", name="T")
        ps.cost_records = [
            CostRecord(
                agent=AgentRole.RESEARCHER, provider=LLMProvider.CLAUDE,
                model="s", input_tokens=100, output_tokens=50,
                cost_usd=0.01, task_description="t", stage=Stage.PROBLEM_DEFINITION,
            ),
            CostRecord(
                agent=AgentRole.RESEARCHER, provider=LLMProvider.CLAUDE,
                model="s", input_tokens=100, output_tokens=50,
                cost_usd=0.05, task_description="t2", stage=Stage.LITERATURE_REVIEW,
            ),
        ]
        assert abs(ps.stage_cost(Stage.PROBLEM_DEFINITION) - 0.01) < 1e-9

    def test_record_event(self):
        ps = ProjectState(project_id="t", name="T")
        ps.record_event(
            VersionEventType.AGENT_RUN,
            summary="Researcher produced output",
            agent=AgentRole.RESEARCHER,
        )
        assert len(ps.timeline) == 1
        assert ps.timeline[0].version == "0.1"
        assert ps.timeline[0].event_type == VersionEventType.AGENT_RUN

    def test_record_transition_forward(self):
        ps = ProjectState(project_id="t", name="T")
        ps.record_transition(Stage.LITERATURE_REVIEW, "problem_defined")
        assert ps.current_stage == Stage.LITERATURE_REVIEW
        assert len(ps.transitions) == 1
        assert ps.transitions[0].trigger == "problem_defined"
        # Should have recorded a STAGE_ADVANCE event
        advance_events = [e for e in ps.timeline if e.event_type == VersionEventType.STAGE_ADVANCE]
        assert len(advance_events) == 1

    def test_record_transition_backward_increments_iteration(self):
        ps = ProjectState(
            project_id="t", name="T",
            current_stage=Stage.HYPOTHESIS_FORMATION,
        )
        ps.record_transition(Stage.LITERATURE_REVIEW, "need_more_evidence")
        assert ps.current_stage == Stage.LITERATURE_REVIEW
        assert ps.iteration_count.get("literature_review", 1) == 2
        rollback_events = [e for e in ps.timeline if e.event_type == VersionEventType.STAGE_ROLLBACK]
        assert len(rollback_events) == 1
