"""Tests for research_agent.prechecks — pre-review structural checks."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_agent.models import (
    AgentRole,
    Artifact,
    ArtifactType,
    ProjectState,
    Stage,
)
from research_agent.prechecks import (
    get_completion_percentage,
    pre_review_checks,
    verify_backend_capabilities,
)
from research_agent.models import CLIBackend
from research_agent.state import StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_project(sm: StateManager, tmp_base: Path) -> tuple[ProjectState, str]:
    state = sm.create_project("Test")
    return state, state.project_id


def _add_artifact(state: ProjectState, sm: StateManager, pid: str,
                  atype: ArtifactType, stage: Stage, content: str,
                  version: int = 1) -> Artifact:
    filename = f"{atype.value}_v{version}.yaml"
    sm.save_artifact_file(pid, stage, filename, content)
    art = Artifact(
        name=f"{atype.value}_v{version}",
        artifact_type=atype,
        stage=stage,
        version=version,
        path=f"artifacts/{stage.value}/{filename}",
        created_by=AgentRole.RESEARCHER,
    )
    state.artifacts.append(art)
    return art


# ---------------------------------------------------------------------------
# Literature review checks
# ---------------------------------------------------------------------------

class TestLiteratureChecks:
    def test_missing_urls(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        content = yaml.dump({
            "papers": [
                {"title": "Paper A"},
                {"title": "Paper B", "url": "https://example.com"},
                {"title": "Paper C"},
                {"title": "Paper D"},
                {"title": "Paper E"},
            ],
        })
        _add_artifact(state, state_manager, pid, ArtifactType.LITERATURE_MAP,
                      Stage.LITERATURE_REVIEW, content)
        issues = pre_review_checks(state, Stage.LITERATURE_REVIEW, state_manager, pid, tmp_base)
        assert any("no URL" in i for i in issues)

    def test_too_few_papers(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        content = yaml.dump({
            "papers": [
                {"title": "Paper A", "url": "https://a.com"},
                {"title": "Paper B", "url": "https://b.com"},
            ],
        })
        _add_artifact(state, state_manager, pid, ArtifactType.LITERATURE_MAP,
                      Stage.LITERATURE_REVIEW, content)
        issues = pre_review_checks(state, Stage.LITERATURE_REVIEW, state_manager, pid, tmp_base)
        assert any("5+" in i or "need 5" in i.lower() for i in issues)

    def test_good_literature(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        papers = [{"title": f"Paper {i}", "url": f"https://p{i}.com"} for i in range(6)]
        content = yaml.dump({"papers": papers})
        _add_artifact(state, state_manager, pid, ArtifactType.LITERATURE_MAP,
                      Stage.LITERATURE_REVIEW, content)
        issues = pre_review_checks(state, Stage.LITERATURE_REVIEW, state_manager, pid, tmp_base)
        assert issues == []

    def test_unverified_papers(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        papers = [
            {"title": f"P{i}", "url": f"https://p{i}.com", "verified": False}
            for i in range(6)
        ]
        content = yaml.dump({"papers": papers})
        _add_artifact(state, state_manager, pid, ArtifactType.LITERATURE_MAP,
                      Stage.LITERATURE_REVIEW, content)
        issues = pre_review_checks(state, Stage.LITERATURE_REVIEW, state_manager, pid, tmp_base)
        assert any("unverified" in i.lower() for i in issues)


# ---------------------------------------------------------------------------
# Implementation checks
# ---------------------------------------------------------------------------

class TestImplementationChecks:
    def test_dummy_dataset_detected(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        code_content = yaml.dump({
            "files": [{"path": "train.py", "content": "class DummyDataset:\n    pass"}],
        })
        _add_artifact(state, state_manager, pid, ArtifactType.CODE,
                      Stage.IMPLEMENTATION, code_content)
        issues = pre_review_checks(state, Stage.IMPLEMENTATION, state_manager, pid, tmp_base)
        assert any("DummyDataset" in i for i in issues)

    def test_random_tensor_dataset(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        code_content = yaml.dump({
            "files": [{"path": "data.py", "content": "dataset = torch.randn(100, 3)"}],
        })
        _add_artifact(state, state_manager, pid, ArtifactType.CODE,
                      Stage.IMPLEMENTATION, code_content)
        issues = pre_review_checks(state, Stage.IMPLEMENTATION, state_manager, pid, tmp_base)
        assert any("random tensors" in i.lower() for i in issues)

    def test_verified_test_failure(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        tr_content = yaml.dump({
            "overall_status": "some_failed",
            "verified_by": "orchestrator",
        })
        _add_artifact(state, state_manager, pid, ArtifactType.TEST_RESULT,
                      Stage.IMPLEMENTATION, tr_content)
        issues = pre_review_checks(state, Stage.IMPLEMENTATION, state_manager, pid, tmp_base)
        assert any("VERIFIED" in i and "FAILED" in i for i in issues)


# ---------------------------------------------------------------------------
# Analysis checks
# ---------------------------------------------------------------------------

class TestAnalysisChecks:
    def test_low_completion(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        cc_content = yaml.dump({
            "overall_status": {"completion_percentage": "30%"},
        })
        _add_artifact(state, state_manager, pid, ArtifactType.CLAIM_CHECKLIST,
                      Stage.ANALYSIS, cc_content)
        issues = pre_review_checks(state, Stage.ANALYSIS, state_manager, pid, tmp_base)
        assert any("30" in i for i in issues)


# ---------------------------------------------------------------------------
# get_completion_percentage
# ---------------------------------------------------------------------------

class TestGetCompletionPercentage:
    def test_with_percentage_string(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        content = yaml.dump({"overall_status": {"completion_percentage": "75%"}})
        _add_artifact(state, state_manager, pid, ArtifactType.CLAIM_CHECKLIST,
                      Stage.ANALYSIS, content)
        assert get_completion_percentage(state, state_manager, pid) == 75.0

    def test_with_numeric(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        content = yaml.dump({"overall_status": {"completion_percentage": 90}})
        _add_artifact(state, state_manager, pid, ArtifactType.CLAIM_CHECKLIST,
                      Stage.ANALYSIS, content)
        assert get_completion_percentage(state, state_manager, pid) == 90

    def test_no_checklist(self, state_manager: StateManager, tmp_base: Path):
        state, pid = _setup_project(state_manager, tmp_base)
        assert get_completion_percentage(state, state_manager, pid) is None


# ---------------------------------------------------------------------------
# verify_backend_capabilities
# ---------------------------------------------------------------------------

class TestVerifyBackendCapabilities:
    def test_researcher_non_claude_lit_review(self):
        warnings = verify_backend_capabilities(
            CLIBackend.OPENCODE, AgentRole.RESEARCHER, Stage.LITERATURE_REVIEW,
        )
        assert any("WebSearch" in w for w in warnings)

    def test_researcher_claude_ok(self):
        warnings = verify_backend_capabilities(
            CLIBackend.CLAUDE, AgentRole.RESEARCHER, Stage.LITERATURE_REVIEW,
        )
        # Claude researcher should not have WebSearch warning
        assert not any("WebSearch" in w for w in warnings)

    def test_critic_non_codex_warning(self):
        warnings = verify_backend_capabilities(
            CLIBackend.CLAUDE, AgentRole.CRITIC, Stage.PROBLEM_DEFINITION,
        )
        assert any("write files" in w.lower() for w in warnings)

    def test_critic_codex_ok(self):
        warnings = verify_backend_capabilities(
            CLIBackend.CODEX, AgentRole.CRITIC, Stage.PROBLEM_DEFINITION,
        )
        assert not any("write files" in w.lower() for w in warnings)
