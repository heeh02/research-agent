"""Tests for research_agent.gate_eval — layered gate verdict evaluation."""
from __future__ import annotations

import pytest

from research_agent.gate_eval import evaluate_gate_verdict, GateVerdict


class TestEvaluateGateVerdict:
    """Test the three-layer verdict evaluation pipeline."""

    # --- Layer 1: critic verdict parsing ---

    def test_critic_pass_no_overrides(self):
        """PASS with no pre-check issues and no criteria → stays PASS."""
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=[],
        )
        assert gv.verdict == "PASS"
        assert gv.critic_verdict == "PASS"
        assert not gv.pre_check_override
        assert not gv.score_override

    def test_critic_revise_stays_revise(self):
        """REVISE from critic is never upgraded."""
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: REVISE\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=[],
        )
        assert gv.verdict == "REVISE"
        assert gv.critic_verdict == "REVISE"

    def test_critic_fail_stays_fail(self):
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: FAIL\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=[],
        )
        assert gv.verdict == "FAIL"

    def test_empty_output_fails(self):
        gv = evaluate_gate_verdict("", False, [], [])
        assert gv.verdict == "FAIL"

    def test_no_verdict_defaults_to_revise(self):
        gv = evaluate_gate_verdict("some random text", True, [], [])
        assert gv.verdict == "REVISE"

    # --- Layer 2: pre-check override ---

    def test_precheck_overrides_pass_to_revise(self):
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```',
            critic_success=True,
            pre_check_issues=["DummyDataset found", "Missing URLs"],
            stage_criteria=[],
        )
        assert gv.verdict == "REVISE"
        assert gv.critic_verdict == "PASS"
        assert gv.pre_check_override
        assert len(gv.pre_check_issues) == 2
        assert "PRE-CHECK OVERRIDE" in gv.annotation

    def test_precheck_does_not_override_revise(self):
        """Pre-checks only override PASS, not REVISE/FAIL."""
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: REVISE\n```',
            critic_success=True,
            pre_check_issues=["some issue"],
            stage_criteria=[],
        )
        assert gv.verdict == "REVISE"
        assert not gv.pre_check_override  # was already REVISE

    def test_precheck_does_not_override_fail(self):
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: FAIL\n```',
            critic_success=True,
            pre_check_issues=["some issue"],
            stage_criteria=[],
        )
        assert gv.verdict == "FAIL"
        assert not gv.pre_check_override

    # --- Layer 3: weighted score override ---

    def test_low_scores_override_pass(self):
        criteria = [
            {"name": "rigor", "weight": 0.5},
            {"name": "completeness", "weight": 0.5},
        ]
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.5\n  completeness: 0.5\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=criteria,
            pass_threshold=0.7,
        )
        assert gv.verdict == "REVISE"
        assert gv.score_override
        assert abs(gv.weighted_avg - 0.5) < 1e-9

    def test_high_scores_keep_pass(self):
        criteria = [
            {"name": "rigor", "weight": 0.5},
            {"name": "completeness", "weight": 0.5},
        ]
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n  completeness: 0.8\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=criteria,
            pass_threshold=0.7,
        )
        assert gv.verdict == "PASS"
        assert not gv.score_override

    def test_missing_scores_count_as_zero(self):
        """Missing scores = 0.0, so weighted avg drops below threshold."""
        criteria = [
            {"name": "rigor", "weight": 0.5},
            {"name": "completeness", "weight": 0.5},
        ]
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=criteria,
            pass_threshold=0.7,
        )
        assert gv.verdict == "REVISE"
        assert gv.score_override
        assert abs(gv.weighted_avg - 0.45) < 1e-9  # 0.9*0.5 + 0.0*0.5

    def test_score_check_skipped_when_already_revise(self):
        """If pre-checks already downgraded to REVISE, score check is skipped."""
        criteria = [{"name": "rigor", "weight": 1.0}]
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```',
            critic_success=True,
            pre_check_issues=["blocking issue"],
            stage_criteria=criteria,
        )
        assert gv.verdict == "REVISE"
        assert gv.pre_check_override
        assert not gv.score_override  # skipped because verdict was already REVISE

    # --- Combined layers ---

    def test_all_layers_pass(self):
        criteria = [{"name": "rigor", "weight": 1.0}]
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\nscores:\n  rigor: 0.9\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=criteria,
            pass_threshold=0.7,
        )
        assert gv.verdict == "PASS"
        assert not gv.pre_check_override
        assert not gv.score_override

    def test_empty_criteria_skips_score_check(self):
        gv = evaluate_gate_verdict(
            critic_output='```yaml\nverdict: PASS\n```',
            critic_success=True,
            pre_check_issues=[],
            stage_criteria=[],
        )
        assert gv.verdict == "PASS"
        assert not gv.score_override
