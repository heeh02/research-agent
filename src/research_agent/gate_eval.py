"""Unified gate verdict evaluation.

This module is the SINGLE place where critic output + pre-checks + weighted
scores are combined into a final gate verdict.  Both the CLI path
(multi_agent.py) and the GUI path (gui.py) call this instead of duplicating
the override logic inline.

Key invariant preserved: the final verdict can only be *downgraded* by each
successive layer (PASS → REVISE or FAIL), never upgraded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .verdict import parse_verdict, parse_scores, evaluate_weighted_scores


@dataclass
class GateVerdict:
    """Result of the layered gate evaluation."""
    verdict: str                        # PASS, REVISE, or FAIL
    critic_verdict: str                 # What the critic originally said
    pre_check_override: bool = False    # True if pre-checks downgraded the verdict
    score_override: bool = False        # True if weighted scores downgraded the verdict
    pre_check_issues: list[str] = field(default_factory=list)
    weighted_avg: float = 0.0
    annotation: str = ""                # Text appended to critic output on override


def evaluate_gate_verdict(
    critic_output: str,
    critic_success: bool,
    pre_check_issues: list[str],
    stage_criteria: list[dict[str, Any]],
    pass_threshold: float = 0.7,
) -> GateVerdict:
    """Evaluate the final gate verdict through three layers.

    Layer 1 — Critic verdict parsing (verdict.parse_verdict).
    Layer 2 — Pre-check override: if structural issues exist and critic said
              PASS, downgrade to REVISE.
    Layer 3 — Weighted score check: if weighted average of critic scores is
              below threshold and critic said PASS, downgrade to REVISE.

    Args:
        critic_output: Raw text output from the critic agent.
        critic_success: Whether the critic process exited successfully.
        pre_check_issues: Blocking issues from prechecks.pre_review_checks().
        stage_criteria: Gate criteria list from stages.yaml, e.g.
            [{"name": "rigor", "weight": 0.2}, ...].  Empty list = skip.
        pass_threshold: Minimum weighted score average to keep PASS (default 0.7).

    Returns:
        GateVerdict with the final verdict and per-layer audit trail.
    """
    # Layer 1: parse critic output
    critic_verdict = parse_verdict(critic_output, critic_success)
    verdict = critic_verdict
    gv = GateVerdict(
        verdict=verdict,
        critic_verdict=critic_verdict,
    )

    # Layer 2: pre-check override
    if pre_check_issues and verdict == "PASS":
        verdict = "REVISE"
        gv.pre_check_override = True
        gv.pre_check_issues = list(pre_check_issues)
        gv.annotation = (
            "\n\n--- AUTOMATED PRE-CHECK OVERRIDE ---\n"
            "Verdict overridden to REVISE due to structural issues:\n"
            + "\n".join(f"  - {i}" for i in pre_check_issues)
        )

    # Layer 3: weighted score check
    if stage_criteria and verdict == "PASS":
        scores = parse_scores(critic_output)
        passed, weighted_avg = evaluate_weighted_scores(
            scores, stage_criteria, pass_threshold,
        )
        gv.weighted_avg = weighted_avg
        if not passed:
            verdict = "REVISE"
            gv.score_override = True

    gv.verdict = verdict
    return gv
