"""Tests for research_agent.verdict — verdict parsing, scoring, rollback."""
from __future__ import annotations

import pytest

from research_agent.models import (
    ALLOWED_TRANSITIONS,
    GateResult,
    GateStatus,
    ProjectState,
    Stage,
)
from research_agent.verdict import (
    _normalize_verdict,
    evaluate_rollback,
    evaluate_weighted_scores,
    parse_failure_type,
    parse_scores,
    parse_verdict,
)


# ---------------------------------------------------------------------------
# parse_verdict
# ---------------------------------------------------------------------------

class TestParseVerdict:
    def test_empty_output(self):
        assert parse_verdict("", True) == "FAIL"
        assert parse_verdict("", False) == "FAIL"
        assert parse_verdict("   ", True) == "FAIL"

    def test_yaml_block_pass(self):
        text = '```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```'
        assert parse_verdict(text, True) == "PASS"

    def test_yaml_block_fail(self):
        text = '```yaml\nverdict: FAIL\n```'
        assert parse_verdict(text, True) == "FAIL"

    def test_yaml_block_revise(self):
        text = '```yaml\nverdict: REVISE\n```'
        assert parse_verdict(text, True) == "REVISE"

    def test_yaml_block_accept(self):
        text = '```yaml\nverdict: ACCEPT\n```'
        assert parse_verdict(text, True) == "PASS"

    def test_yaml_block_reject(self):
        text = '```yaml\nverdict: REJECT\n```'
        assert parse_verdict(text, True) == "FAIL"

    def test_yaml_block_conditional_accept(self):
        text = '```yaml\nverdict: CONDITIONAL_ACCEPT\n```'
        assert parse_verdict(text, True) == "REVISE"

    def test_text_pattern_pass(self):
        assert parse_verdict("VERDICT: PASS", True) == "PASS"
        assert parse_verdict("some text\nVERDICT:PASS\nmore text", True) == "PASS"

    def test_text_pattern_fail(self):
        assert parse_verdict("VERDICT: FAIL", True) == "FAIL"
        assert parse_verdict("VERDICT: REJECT", True) == "FAIL"

    def test_text_pattern_revise(self):
        assert parse_verdict("VERDICT: REVISE", True) == "REVISE"

    def test_no_verdict_defaults_to_revise(self):
        """Key invariant: never defaults to PASS."""
        assert parse_verdict("Some random output without verdict", True) == "REVISE"

    def test_case_insensitive_yaml(self):
        text = '```yml\nverdict: pass\n```'
        assert parse_verdict(text, True) == "PASS"

    def test_invalid_yaml_falls_through(self):
        text = '```yaml\n{invalid yaml{{\n```\nVERDICT: PASS'
        assert parse_verdict(text, True) == "PASS"

    def test_yaml_without_verdict_field(self):
        text = '```yaml\nscores:\n  rigor: 0.9\n```'
        assert parse_verdict(text, True) == "REVISE"


# ---------------------------------------------------------------------------
# Multi-YAML-block handling
# ---------------------------------------------------------------------------

class TestMultiYamlBlocks:
    def test_verdict_in_second_block(self):
        """When critic output has artifact block + verdict block, pick the verdict."""
        text = (
            "Here is the artifact:\n"
            "```yaml\n"
            "claim: some hypothesis\n"
            "motivation: testing\n"
            "```\n\n"
            "My review:\n"
            "```yaml\n"
            "verdict: PASS\n"
            "scores:\n"
            "  rigor: 0.8\n"
            "  completeness: 0.9\n"
            "```"
        )
        assert parse_verdict(text, True) == "PASS"

    def test_scores_from_verdict_block_not_artifact(self):
        """Scores should come from the verdict block, not an artifact block."""
        text = (
            "```yaml\n"
            "papers:\n"
            "  - title: Paper A\n"
            "    scores: {impact: 0.5}\n"
            "```\n\n"
            "```yaml\n"
            "verdict: REVISE\n"
            "scores:\n"
            "  rigor: 0.3\n"
            "  completeness: 0.4\n"
            "```"
        )
        scores = parse_scores(text)
        assert scores.get("rigor") == 0.3
        assert scores.get("completeness") == 0.4

    def test_failure_type_from_verdict_block(self):
        """failure_type should be parsed from the verdict block."""
        text = (
            "```yaml\n"
            "experiment_name: exp1\n"
            "```\n\n"
            "```yaml\n"
            "verdict: FAIL\n"
            "failure_type: design_flaw\n"
            "```"
        )
        ft = parse_failure_type(text)
        assert ft == "design_flaw"

    def test_single_block_still_works(self):
        """Backward compat: single block parsing unchanged."""
        text = '```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```'
        assert parse_verdict(text, True) == "PASS"
        assert parse_scores(text) == {"rigor": 0.9}


# ---------------------------------------------------------------------------
# _normalize_verdict
# ---------------------------------------------------------------------------

class TestNormalizeVerdict:
    def test_pass_variants(self):
        assert _normalize_verdict("PASS") == "PASS"
        assert _normalize_verdict("ACCEPT") == "PASS"

    def test_fail_variants(self):
        assert _normalize_verdict("FAIL") == "FAIL"
        assert _normalize_verdict("REJECT") == "FAIL"
        assert _normalize_verdict("MAJOR_REVISION") == "FAIL"

    def test_revise_variants(self):
        assert _normalize_verdict("REVISE") == "REVISE"
        assert _normalize_verdict("MINOR_REVISION") == "REVISE"
        assert _normalize_verdict("CONDITIONAL_ACCEPT") == "REVISE"

    def test_unknown_defaults_to_revise(self):
        assert _normalize_verdict("MAYBE") == "REVISE"
        assert _normalize_verdict("") == "REVISE"


# ---------------------------------------------------------------------------
# parse_scores
# ---------------------------------------------------------------------------

class TestParseScores:
    def test_basic_scores(self):
        text = '```yaml\nverdict: PASS\nscores:\n  rigor: 0.8\n  completeness: 0.9\n```'
        scores = parse_scores(text)
        assert scores == {"rigor": 0.8, "completeness": 0.9}

    def test_nested_score_format(self):
        text = '```yaml\nscores:\n  rigor:\n    score: 0.7\n    comment: ok\n```'
        scores = parse_scores(text)
        assert scores == {"rigor": 0.7}

    def test_no_yaml_block(self):
        assert parse_scores("no yaml here") == {}

    def test_invalid_yaml(self):
        assert parse_scores("```yaml\n{bad{{\n```") == {}

    def test_no_scores_field(self):
        text = '```yaml\nverdict: PASS\n```'
        assert parse_scores(text) == {}

    def test_integer_scores(self):
        text = '```yaml\nscores:\n  rigor: 1\n```'
        scores = parse_scores(text)
        assert scores == {"rigor": 1.0}


# ---------------------------------------------------------------------------
# parse_failure_type
# ---------------------------------------------------------------------------

class TestParseFailureType:
    def test_valid_failure_type(self):
        text = '```yaml\nverdict: FAIL\nfailure_type: structural_issue\n```'
        assert parse_failure_type(text) == "structural_issue"

    def test_cross_stage_failure_type(self):
        text = '```yaml\nfailure_type: design_flaw\n```'
        assert parse_failure_type(text) == "design_flaw"

    def test_invalid_failure_type(self):
        text = '```yaml\nfailure_type: made_up_type\n```'
        assert parse_failure_type(text) is None

    def test_no_yaml(self):
        assert parse_failure_type("no yaml") is None

    def test_no_failure_type_field(self):
        text = '```yaml\nverdict: FAIL\n```'
        assert parse_failure_type(text) is None


# ---------------------------------------------------------------------------
# evaluate_weighted_scores
# ---------------------------------------------------------------------------

class TestEvaluateWeightedScores:
    def test_passing_scores(self):
        criteria = [
            {"name": "rigor", "weight": 0.5},
            {"name": "completeness", "weight": 0.5},
        ]
        scores = {"rigor": 0.8, "completeness": 0.8}
        passed, avg = evaluate_weighted_scores(scores, criteria, threshold=0.7)
        assert passed
        assert abs(avg - 0.8) < 1e-9

    def test_failing_scores(self):
        criteria = [
            {"name": "rigor", "weight": 0.5},
            {"name": "completeness", "weight": 0.5},
        ]
        scores = {"rigor": 0.5, "completeness": 0.5}
        passed, avg = evaluate_weighted_scores(scores, criteria, threshold=0.7)
        assert not passed
        assert abs(avg - 0.5) < 1e-9

    def test_missing_score_counts_as_zero(self):
        """Key invariant: missing scores = 0.0, not skipped."""
        criteria = [
            {"name": "rigor", "weight": 0.5},
            {"name": "completeness", "weight": 0.5},
        ]
        scores = {"rigor": 1.0}  # completeness missing → 0.0
        passed, avg = evaluate_weighted_scores(scores, criteria, threshold=0.7)
        assert not passed  # 0.5 < 0.7
        assert abs(avg - 0.5) < 1e-9

    def test_no_criteria(self):
        passed, avg = evaluate_weighted_scores({"rigor": 0.8}, [], threshold=0.7)
        assert passed
        assert avg == 0.0

    def test_no_scores_with_criteria(self):
        criteria = [{"name": "rigor", "weight": 1.0}]
        passed, avg = evaluate_weighted_scores({}, criteria, threshold=0.7)
        assert not passed
        assert avg == 0.0

    def test_zero_weight_ignored(self):
        criteria = [
            {"name": "rigor", "weight": 1.0},
            {"name": "style", "weight": 0.0},  # should be ignored
        ]
        scores = {"rigor": 0.9, "style": 0.1}
        passed, avg = evaluate_weighted_scores(scores, criteria, threshold=0.7)
        assert passed
        assert abs(avg - 0.9) < 1e-9

    def test_unequal_weights(self):
        criteria = [
            {"name": "a", "weight": 0.8},
            {"name": "b", "weight": 0.2},
        ]
        scores = {"a": 1.0, "b": 0.0}
        passed, avg = evaluate_weighted_scores(scores, criteria, threshold=0.7)
        assert passed  # 0.8/1.0 = 0.8 >= 0.7


# ---------------------------------------------------------------------------
# evaluate_rollback
# ---------------------------------------------------------------------------

class TestEvaluateRollback:
    def _make_gate_result(self, feedback: str) -> GateResult:
        return GateResult(
            gate_name="test",
            stage=Stage.ANALYSIS,
            status=GateStatus.FAILED,
            overall_feedback=feedback,
        )

    def test_structured_same_stage(self):
        """Same-stage failure type → no rollback (returns None)."""
        feedback = '```yaml\nfailure_type: structural_issue\n```'
        gr = self._make_gate_result(feedback)
        ps = ProjectState(project_id="t", name="T", current_stage=Stage.ANALYSIS)
        result = evaluate_rollback(ps, Stage.ANALYSIS, gr)
        assert result is None

    def test_structured_cross_stage(self):
        """Cross-stage failure type → rollback to target."""
        feedback = '```yaml\nfailure_type: hypothesis_falsified\n```'
        gr = self._make_gate_result(feedback)
        ps = ProjectState(project_id="t", name="T", current_stage=Stage.ANALYSIS)
        result = evaluate_rollback(ps, Stage.ANALYSIS, gr)
        assert result == Stage.HYPOTHESIS_FORMATION

    def test_keyword_heuristic_falsified(self):
        feedback = "The hypothesis is fundamentally wrong, hypothesis is falsified."
        gr = self._make_gate_result(feedback)
        ps = ProjectState(project_id="t", name="T", current_stage=Stage.ANALYSIS)
        result = evaluate_rollback(ps, Stage.ANALYSIS, gr)
        assert result == Stage.HYPOTHESIS_FORMATION

    def test_keyword_heuristic_code_bug(self):
        feedback = "There is a code bug causing runtime error in the experiment."
        gr = GateResult(
            gate_name="test", stage=Stage.EXPERIMENTATION,
            status=GateStatus.FAILED, overall_feedback=feedback,
        )
        ps = ProjectState(project_id="t", name="T", current_stage=Stage.EXPERIMENTATION)
        result = evaluate_rollback(ps, Stage.EXPERIMENTATION, gr)
        assert result == Stage.IMPLEMENTATION

    def test_exhausted_target_returns_none(self):
        """If target stage hit max iterations, don't rollback."""
        feedback = '```yaml\nfailure_type: hypothesis_falsified\n```'
        gr = self._make_gate_result(feedback)
        ps = ProjectState(
            project_id="t", name="T",
            current_stage=Stage.ANALYSIS,
            iteration_count={"hypothesis_formation": 5},
        )
        result = evaluate_rollback(ps, Stage.ANALYSIS, gr, max_iterations=5)
        assert result is None

    def test_design_flaw_from_implementation(self):
        feedback = "The design flaw makes this impossible to implement."
        gr = GateResult(
            gate_name="test", stage=Stage.IMPLEMENTATION,
            status=GateStatus.FAILED, overall_feedback=feedback,
        )
        ps = ProjectState(project_id="t", name="T", current_stage=Stage.IMPLEMENTATION)
        result = evaluate_rollback(ps, Stage.IMPLEMENTATION, gr)
        assert result == Stage.EXPERIMENT_DESIGN

    def test_no_rollback_signals(self):
        """Generic feedback with no rollback signals → None."""
        feedback = "The work is mostly fine but needs some polishing."
        gr = GateResult(
            gate_name="test", stage=Stage.PROBLEM_DEFINITION,
            status=GateStatus.FAILED, overall_feedback=feedback,
        )
        ps = ProjectState(project_id="t", name="T")
        result = evaluate_rollback(ps, Stage.PROBLEM_DEFINITION, gr)
        assert result is None
