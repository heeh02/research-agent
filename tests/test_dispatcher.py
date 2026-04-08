"""Tests for research_agent.dispatcher — TaskCard, AgentResult, retry logic, prompt building."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_agent.dispatcher import (
    AgentResult,
    AgentToolset,
    DEFAULT_AGENT_MODELS,
    DEFAULT_MAX_TURNS,
    MultiAgentDispatcher,
    TaskCard,
    _is_auth_error,
    _is_retryable,
    _retry_wait,
)
from research_agent.models import AgentRole, Stage


# ---------------------------------------------------------------------------
# TaskCard
# ---------------------------------------------------------------------------

class TestTaskCard:
    def test_to_yaml_roundtrip(self):
        tc = TaskCard(
            task_id="task-001",
            role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="Define the problem clearly.",
            context_files=["artifacts/problem_definition/brief.yaml"],
            required_outputs=["artifacts/problem_definition/problem_brief_v1.yaml"],
            previous_feedback="Needs more detail.",
            constraints=["Must include 5+ references"],
            metadata={"project_id": "test-123"},
        )
        yaml_str = tc.to_yaml()
        loaded = TaskCard.from_yaml(yaml_str)
        assert loaded.task_id == "task-001"
        assert loaded.role == AgentRole.RESEARCHER
        assert loaded.stage == Stage.PROBLEM_DEFINITION
        assert loaded.instruction == "Define the problem clearly."
        assert len(loaded.context_files) == 1
        assert loaded.previous_feedback == "Needs more detail."

    def test_minimal_task_card(self):
        tc = TaskCard(
            task_id="min",
            role=AgentRole.ENGINEER,
            stage=Stage.IMPLEMENTATION,
            instruction="Write code.",
        )
        yaml_str = tc.to_yaml()
        loaded = TaskCard.from_yaml(yaml_str)
        assert loaded.context_files == []
        assert loaded.constraints == []


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

class TestAgentResult:
    def test_success_result(self):
        r = AgentResult(
            task_id="t", role=AgentRole.RESEARCHER,
            success=True, output_text="Done.",
        )
        assert r.success
        assert r.retries == 0

    def test_failure_result(self):
        r = AgentResult(
            task_id="t", role=AgentRole.ENGINEER,
            success=False, output_text="Error",
            error="Exit code: 1", exit_code=1,
        )
        assert not r.success


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

class TestRetryHelpers:
    def test_retryable_patterns(self):
        assert _is_retryable("403 Forbidden", 1)
        assert _is_retryable("rate limit exceeded", 1)
        assert _is_retryable("connection reset by peer", 1)
        assert _is_retryable("request timed out", 1)
        assert _is_retryable("503 service unavailable", 1)

    def test_not_retryable_on_success(self):
        assert not _is_retryable("403", 0)

    def test_not_retryable_generic_error(self):
        assert not _is_retryable("some random error message", 1)

    def test_auth_error_detection(self):
        assert _is_auth_error("403 Forbidden - please authenticate")
        assert _is_auth_error("please run /login to continue")
        assert not _is_auth_error("normal output")

    def test_retry_wait_exponential(self):
        assert _retry_wait(0) == 10
        assert _retry_wait(1) == 30
        assert _retry_wait(2) == 90
        assert _retry_wait(5) == 300  # Capped at 300


# ---------------------------------------------------------------------------
# MultiAgentDispatcher — unit tests (no subprocess)
# ---------------------------------------------------------------------------

class TestDispatcherInit:
    def test_default_backends(self, tmp_path: Path):
        d = MultiAgentDispatcher(
            project_dir=tmp_path,
            agents_dir=tmp_path / "agents",
        )
        assert d.backends[AgentRole.CRITIC] == "codex"
        assert d.backends[AgentRole.RESEARCHER] == "claude"
        assert d.backends[AgentRole.ENGINEER] == "claude"

    def test_custom_config(self, tmp_path: Path):
        config = {
            "agents": {
                "researcher": {"backend": "opencode", "model": "doubao-pro"},
                "critic": {"backend": "claude", "model": "claude-sonnet-4-20250514"},
            },
        }
        d = MultiAgentDispatcher(
            project_dir=tmp_path,
            agents_dir=tmp_path / "agents",
            config=config,
        )
        assert d.backends[AgentRole.RESEARCHER] == "opencode"
        assert d.backends[AgentRole.CRITIC] == "claude"
        assert d.models[AgentRole.RESEARCHER] == "doubao-pro"


class TestPromptBuilding:
    def test_build_prompt_basic(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "researcher").mkdir(parents=True)
        (agents_dir / "researcher" / "CLAUDE.md").write_text("# Researcher\nDo research.")

        d = MultiAgentDispatcher(
            project_dir=tmp_path,
            agents_dir=agents_dir,
        )
        tc = TaskCard(
            task_id="t1", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="Define the problem.",
            constraints=["Be rigorous"],
            required_outputs=["artifacts/problem_definition/pb_v1.yaml"],
            context_files=["state.json"],
            previous_feedback="Make it better.",
        )
        prompt = d._build_prompt(tc)
        assert "# Researcher" in prompt
        assert "Define the problem." in prompt
        assert "Be rigorous" in prompt
        assert "pb_v1.yaml" in prompt
        assert "Make it better." in prompt

    def test_build_prompt_no_claude_md(self, tmp_path: Path):
        d = MultiAgentDispatcher(
            project_dir=tmp_path,
            agents_dir=tmp_path / "agents",  # no files
        )
        tc = TaskCard(
            task_id="t2", role=AgentRole.ENGINEER,
            stage=Stage.IMPLEMENTATION,
            instruction="Write code.",
        )
        prompt = d._build_prompt(tc)
        assert "Write code." in prompt


class TestToolsetSelection:
    def test_default_toolsets(self, tmp_path: Path):
        d = MultiAgentDispatcher(
            project_dir=tmp_path,
            agents_dir=tmp_path / "agents",
        )
        assert "WebSearch" in d._get_toolset(AgentRole.RESEARCHER)
        assert "Bash" in d._get_toolset(AgentRole.ENGINEER)
        assert "Bash" in d._get_toolset(AgentRole.ORCHESTRATOR)

    def test_config_override_toolset(self, tmp_path: Path):
        config = {
            "agents": {
                "researcher": {"allowed_tools": "Read,Write,Glob"},
            },
        }
        d = MultiAgentDispatcher(
            project_dir=tmp_path,
            agents_dir=tmp_path / "agents",
            config=config,
        )
        assert d._get_toolset(AgentRole.RESEARCHER) == "Read,Write,Glob"


class TestOutputFileDetection:
    def test_detect_expected_files(self, tmp_path: Path):
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        # Create expected file
        (tmp_path / "output.yaml").write_text("data: value")
        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=["output.yaml", "missing.yaml"],
        )
        files = d._detect_output_files(tc, "")
        assert "output.yaml" in files
        assert "missing.yaml" not in files

    def test_detect_files_in_stage_dir(self, tmp_path: Path):
        """Glob fallback with no dispatch_start finds all matching files."""
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)
        (stage_dir / "problem_brief_v1.yaml").write_text("title: Test")

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=[],
            metadata={"project_id": "pid"},
        )
        # dispatch_start=0 → no mtime filtering (backward compat)
        files = d._detect_output_files(tc, "")
        assert len(files) == 1

    def test_stale_artifact_not_promoted(self, tmp_path: Path):
        """P1: Pre-existing files must NOT be returned when dispatch_start is set."""
        import time as _time
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)

        # Create a stale file BEFORE the dispatch
        stale = stage_dir / "problem_brief_v1.yaml"
        stale.write_text("title: Old")

        _time.sleep(0.05)  # Ensure mtime separation
        dispatch_start = _time.time()

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=["projects/pid/artifacts/problem_definition/problem_brief_v2.yaml"],
            metadata={"project_id": "pid"},
        )
        files = d._detect_output_files(tc, "", dispatch_start=dispatch_start)
        # Stale v1 must NOT be returned — expected v2 doesn't exist, glob finds v1
        # but v1.mtime < dispatch_start so it's filtered out
        assert len(files) == 0

    def test_stale_expected_file_not_accepted(self, tmp_path: Path):
        """Pre-existing file at the exact expected path must be rejected by mtime check."""
        import time as _time
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)

        # Stale file exists at the EXACT expected path before dispatch
        expected_path = "projects/pid/artifacts/problem_definition/problem_brief_v2.yaml"
        (tmp_path / expected_path).write_text("title: Stale v2")

        _time.sleep(0.05)
        dispatch_start = _time.time()

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=[expected_path],
            metadata={"project_id": "pid"},
        )
        files = d._detect_output_files(tc, "", dispatch_start=dispatch_start)
        # File exists at expected path but was NOT written during this dispatch
        assert len(files) == 0

    def test_fresh_expected_file_accepted(self, tmp_path: Path):
        """File at the expected path written AFTER dispatch_start is accepted."""
        import time as _time
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)

        dispatch_start = _time.time()
        _time.sleep(0.05)

        expected_path = "projects/pid/artifacts/problem_definition/problem_brief_v2.yaml"
        (tmp_path / expected_path).write_text("title: Fresh v2")

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=[expected_path],
            metadata={"project_id": "pid"},
        )
        files = d._detect_output_files(tc, "", dispatch_start=dispatch_start)
        assert files == [expected_path]

    def test_new_file_detected_by_glob(self, tmp_path: Path):
        """Glob fallback correctly detects files written AFTER dispatch_start."""
        import time as _time
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)

        dispatch_start = _time.time()
        _time.sleep(0.05)

        # Write a new file AFTER dispatch_start
        (stage_dir / "problem_brief_v1.yaml").write_text("title: New")

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=["projects/pid/artifacts/problem_definition/problem_brief_v2.yaml"],
            metadata={"project_id": "pid"},
        )
        files = d._detect_output_files(tc, "", dispatch_start=dispatch_start)
        assert len(files) == 1
        assert "problem_brief_v1" in files[0]

    def test_non_canonical_name_excluded(self, tmp_path: Path):
        """Files that don't match any stage-required ArtifactType are excluded."""
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "analysis"
        stage_dir.mkdir(parents=True)
        # "analysis.yaml" doesn't match any ArtifactType value
        (stage_dir / "analysis.yaml").write_text("title: Misc")
        # "result_report_v1.yaml" matches "result_report" which is required for analysis
        (stage_dir / "result_report_v1.yaml").write_text("title: Report")

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.ANALYSIS,
            instruction="test",
            required_outputs=[],
            metadata={"project_id": "pid"},
        )
        files = d._detect_output_files(tc, "")
        assert len(files) == 1
        assert "result_report_v1" in files[0]

    def test_wrong_stage_artifact_type_excluded(self, tmp_path: Path):
        """A fresh code_v1.yaml in problem_definition stage must be rejected."""
        d = MultiAgentDispatcher(project_dir=tmp_path, agents_dir=tmp_path / "agents")
        stage_dir = tmp_path / "projects" / "pid" / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)
        # "code_v1.yaml" matches ArtifactType.CODE but is NOT required for problem_definition
        (stage_dir / "code_v1.yaml").write_text("files: []")

        tc = TaskCard(
            task_id="t", role=AgentRole.RESEARCHER,
            stage=Stage.PROBLEM_DEFINITION,
            instruction="test",
            required_outputs=[],
            metadata={"project_id": "pid"},
        )
        files = d._detect_output_files(tc, "")
        # problem_definition requires [problem_brief], not [code]
        assert len(files) == 0


class TestYamlToReadable:
    def test_string_value(self):
        raw = yaml.dump({"title": "Hello World"})
        readable = MultiAgentDispatcher._yaml_to_readable("test.yaml", raw)
        assert "Hello World" in readable
        assert "## test.yaml" in readable

    def test_list_value(self):
        raw = yaml.dump({"items": ["a", "b", "c"]})
        readable = MultiAgentDispatcher._yaml_to_readable("test.yaml", raw)
        assert "1. a" in readable

    def test_dict_value(self):
        raw = yaml.dump({"config": {"key": "value", "num": 42}})
        readable = MultiAgentDispatcher._yaml_to_readable("test.yaml", raw)
        assert "key: value" in readable

    def test_invalid_yaml(self):
        readable = MultiAgentDispatcher._yaml_to_readable("bad.yaml", "not: valid: yaml: {{{")
        assert "## bad.yaml" in readable


# ---------------------------------------------------------------------------
# Cost parsing
# ---------------------------------------------------------------------------

class TestParsClaudeJson:
    def test_real_claude_output(self):
        """Parse a real Claude CLI JSON output with usage data."""
        raw = (
            '{"type":"result","subtype":"success","is_error":false,'
            '"duration_ms":3373,"num_turns":1,"result":"hello world",'
            '"total_cost_usd":0.0299,"usage":{"input_tokens":10,'
            '"cache_creation_input_tokens":100,"cache_read_input_tokens":200,'
            '"output_tokens":37}}'
        )
        text, exit_hint, cost, in_tok, out_tok = MultiAgentDispatcher._parse_claude_json(raw)
        assert text == "hello world"
        assert exit_hint == 0
        assert abs(cost - 0.0299) < 1e-6
        assert in_tok == 310  # 10 + 100 + 200
        assert out_tok == 37

    def test_error_output(self):
        raw = '{"type":"result","is_error":true,"result":"Auth failed","total_cost_usd":0.0,"usage":{"input_tokens":0,"output_tokens":0}}'
        text, exit_hint, cost, in_tok, out_tok = MultiAgentDispatcher._parse_claude_json(raw)
        assert text == "Auth failed"
        assert exit_hint == 1
        assert cost == 0.0

    def test_invalid_json(self):
        text, exit_hint, cost, in_tok, out_tok = MultiAgentDispatcher._parse_claude_json("not json")
        assert text == "not json"
        assert exit_hint == -1
        assert cost == 0.0

    def test_missing_usage_fields(self):
        raw = '{"result":"ok","total_cost_usd":0.05,"usage":{}}'
        text, exit_hint, cost, in_tok, out_tok = MultiAgentDispatcher._parse_claude_json(raw)
        assert text == "ok"
        assert abs(cost - 0.05) < 1e-6
        assert in_tok == 0
        assert out_tok == 0

    def test_null_cost(self):
        raw = '{"result":"ok","total_cost_usd":null,"usage":{"input_tokens":null,"output_tokens":5}}'
        text, exit_hint, cost, in_tok, out_tok = MultiAgentDispatcher._parse_claude_json(raw)
        assert cost == 0.0
        assert in_tok == 0
        assert out_tok == 5


class TestEstimateCost:
    def test_basic_estimate(self):
        cost, in_tok, out_tok = MultiAgentDispatcher._estimate_cost_from_text(
            "a" * 400, "b" * 200, "unknown-model")
        assert in_tok == 100  # 400 / 4
        assert out_tok == 50   # 200 / 4
        assert cost > 0

    def test_gpt_pricing(self):
        cost_gpt, _, _ = MultiAgentDispatcher._estimate_cost_from_text(
            "a" * 4000, "b" * 4000, "gpt-5.4")
        cost_default, _, _ = MultiAgentDispatcher._estimate_cost_from_text(
            "a" * 4000, "b" * 4000, "unknown-model")
        # GPT-5.4 is more expensive than default
        assert cost_gpt > cost_default

    def test_empty_input(self):
        cost, in_tok, out_tok = MultiAgentDispatcher._estimate_cost_from_text("", "", "x")
        assert in_tok >= 1  # min 1 token
        assert out_tok >= 1
        assert cost > 0


class TestAgentResultCostFields:
    def test_new_fields_present(self):
        r = AgentResult(
            task_id="t", role=AgentRole.RESEARCHER,
            success=True, output_text="done",
            cost_usd=0.05, input_tokens=100, output_tokens=50,
            cost_source="claude_cli",
        )
        assert r.cost_usd == 0.05
        assert r.input_tokens == 100
        assert r.output_tokens == 50
        assert r.cost_source == "claude_cli"

    def test_default_cost_fields(self):
        r = AgentResult(task_id="t", role=AgentRole.RESEARCHER,
                        success=True, output_text="done")
        assert r.cost_usd == 0.0
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.cost_source == "unknown"
