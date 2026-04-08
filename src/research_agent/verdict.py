"""Shared verdict parsing, weighted scoring, and rollback evaluation.

This module is the SINGLE SOURCE OF TRUTH for interpreting critic output.
Both multi_agent.py (CLI path) and gui.py (web path) must use these functions
instead of inline verdict parsing.

Key invariant: parse_verdict() NEVER defaults to "PASS". If no clear verdict
is found, it returns "REVISE".
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

import yaml

from .models import (
    ALLOWED_TRANSITIONS,
    ArtifactType,
    GateResult,
    ProjectState,
    Stage,
    STAGE_ORDER,
)

if TYPE_CHECKING:
    from .state import StateManager


# ---------------------------------------------------------------------------
# Failure type taxonomy — drives rollback routing
# ---------------------------------------------------------------------------

# Same-stage failures: critic says REVISE/FAIL but stay in current stage
FAILURE_TYPE_SAME_STAGE = frozenset({
    "structural_issue",     # YAML invalid, missing fields, schema violations
    "implementation_bug",   # Code crashes, tests fail, DummyDataset
    "analysis_gap",         # Analysis incomplete, claims not grounded
})

# Cross-stage failures: rollback to a different stage
FAILURE_TYPE_CROSS_STAGE: dict[str, Stage] = {
    "design_flaw":                Stage.EXPERIMENT_DESIGN,
    "hypothesis_needs_revision":  Stage.HYPOTHESIS_FORMATION,
    "evidence_insufficient":      Stage.EXPERIMENTATION,
    "hypothesis_falsified":       Stage.HYPOTHESIS_FORMATION,
}

ALL_FAILURE_TYPES = FAILURE_TYPE_SAME_STAGE | frozenset(FAILURE_TYPE_CROSS_STAGE.keys())


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

def parse_verdict(output_text: str, process_success: bool) -> str:
    """Parse verdict from critic output. Returns 'PASS', 'REVISE', or 'FAIL'.

    Logic (strictly ordered):
    1. Try to extract YAML block and read verdict field
    2. Fall back to VERDICT: X text pattern matching
    3. If no verdict found and process produced output → REVISE (never PASS)
    4. If process crashed with no output → FAIL
    """
    if not output_text or not output_text.strip():
        return "FAIL"

    # Step 1: Try YAML block extraction — picks the block with 'verdict' key,
    # not just the first block (which might be a quoted artifact).
    block = _find_verdict_yaml_block(output_text)
    if block:
        try:
            data = yaml.safe_load(block)
            if isinstance(data, dict) and "verdict" in data:
                raw = str(data["verdict"]).upper().strip()
                return _normalize_verdict(raw)
        except (yaml.YAMLError, ValueError):
            pass

    # Step 2: Text pattern matching (case-insensitive)
    upper = output_text.upper()
    for pattern, verdict in [
        ("VERDICT: PASS", "PASS"),
        ("VERDICT:PASS", "PASS"),
        ("VERDICT: FAIL", "FAIL"),
        ("VERDICT:FAIL", "FAIL"),
        ("VERDICT: REJECT", "FAIL"),
        ("VERDICT:REJECT", "FAIL"),
        ("VERDICT: REVISE", "REVISE"),
        ("VERDICT:REVISE", "REVISE"),
    ]:
        if pattern in upper:
            return verdict

    # Step 3: No clear verdict found — default to REVISE (NEVER PASS)
    return "REVISE"


def _normalize_verdict(raw: str) -> str:
    """Normalize non-standard verdict strings."""
    if raw in ("PASS", "ACCEPT"):
        return "PASS"
    if raw in ("FAIL", "REJECT", "MAJOR_REVISION"):
        return "FAIL"
    if raw in ("REVISE", "MINOR_REVISION", "CONDITIONAL_ACCEPT"):
        return "REVISE"
    # Unknown → REVISE (safe default)
    return "REVISE"


def _find_verdict_yaml_block(text: str) -> Optional[str]:
    """Find the YAML block most likely to contain the verdict.

    When critic output has multiple YAML blocks (e.g., quoted artifact + verdict),
    re.search would return the first block which may be an artifact quote.

    Strategy:
    1. Find ALL yaml blocks via re.findall
    2. Return the first block that contains a 'verdict' key
    3. If none contains 'verdict', return the LAST block (most likely the verdict)
    4. If no blocks at all, return None
    """
    blocks = re.findall(r"```ya?ml\s*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return None

    # Prefer the block that has a verdict key
    for block in blocks:
        try:
            data = yaml.safe_load(block)
            if isinstance(data, dict) and "verdict" in data:
                return block
        except (yaml.YAMLError, ValueError):
            continue

    # No block has verdict — return the last one
    return blocks[-1]


def parse_scores(output_text: str) -> dict[str, float]:
    """Extract review scores from critic output YAML block.

    Returns dict like {"rigor": 0.8, "completeness": 0.7, ...}.
    Empty dict if no scores found.
    """
    block = _find_verdict_yaml_block(output_text)
    if not block:
        return {}
    try:
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            return {}
        raw_scores = data.get("scores", {})
        scores: dict[str, float] = {}
        for k, v in raw_scores.items():
            if isinstance(v, (int, float)):
                scores[k] = float(v)
            elif isinstance(v, dict) and "score" in v:
                scores[k] = float(v["score"])
        return scores
    except (yaml.YAMLError, ValueError, TypeError):
        return {}


def parse_failure_type(output_text: str) -> Optional[str]:
    """Extract failure_type from critic output YAML block.

    Returns the failure_type string if found and valid, else None.
    When None, callers should fall back to keyword-based rollback heuristics.
    """
    block = _find_verdict_yaml_block(output_text)
    if not block:
        return None
    try:
        data = yaml.safe_load(block)
        if isinstance(data, dict):
            ft = str(data.get("failure_type", "")).strip()
            if ft and ft in ALL_FAILURE_TYPES:
                return ft
    except (yaml.YAMLError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Weighted score evaluation (uses config/stages.yaml criteria)
# ---------------------------------------------------------------------------

def evaluate_weighted_scores(
    scores: dict[str, float],
    criteria: list[dict[str, Any]],
    threshold: float = 0.7,
) -> tuple[bool, float]:
    """Evaluate scores against weighted criteria from stages.yaml.

    Args:
        scores: {"rigor": 0.8, "completeness": 0.6, ...} from critic
        criteria: [{"name": "rigor", "weight": 0.2}, ...] from stages.yaml
        threshold: minimum weighted average to pass (default 0.7)

    Returns:
        (passed, weighted_average)

    IMPORTANT: Missing score fields count as 0.0, NOT skipped.
    This prevents bypassing the gate by omitting low-scoring criteria.
    """
    if not criteria:
        return True, 0.0  # No criteria configured → skip
    if not scores:
        return False, 0.0  # Criteria exist but no scores at all → fail

    total_weight = 0.0
    weighted_sum = 0.0
    for crit in criteria:
        name = crit.get("name", "")
        weight = float(crit.get("weight", 0.0))
        if weight <= 0:
            continue
        # Missing scores count as 0.0 — cannot bypass by omission
        score = scores.get(name, 0.0)
        weighted_sum += score * weight
        total_weight += weight

    if total_weight == 0:
        return True, 0.0

    weighted_avg = weighted_sum / total_weight
    return weighted_avg >= threshold, weighted_avg


# ---------------------------------------------------------------------------
# Automatic backward transition evaluation
# ---------------------------------------------------------------------------

def evaluate_rollback(
    state: ProjectState,
    stage: Stage,
    gate_result: GateResult,
    max_iterations: int = 5,
    state_manager: Optional["StateManager"] = None,
    project_id: Optional[str] = None,
) -> Optional[Stage]:
    """Determine if an automatic backward transition is appropriate.

    Two-layer decision:
    1. PRIMARY: Use structured failure_type from critic (if present).
       - Same-stage types (structural_issue, implementation_bug, analysis_gap)
         return None (caller should do same-stage revise).
       - Cross-stage types map directly to rollback targets.
    2. FALLBACK: If no failure_type, use keyword heuristics on feedback text.

    Safety: if target stage iteration_count >= max_iterations, don't rollback.
    """
    feedback = gate_result.overall_feedback or ""

    # --- Primary: structured failure_type ---
    ft = parse_failure_type(feedback)
    if ft:
        if ft in FAILURE_TYPE_SAME_STAGE:
            return None  # Same-stage revise, no rollback
        if ft in FAILURE_TYPE_CROSS_STAGE:
            target = FAILURE_TYPE_CROSS_STAGE[ft]
            if _is_rollback_allowed(state, stage, target, max_iterations):
                return target
            return None  # Target exhausted

    # --- Fallback: keyword heuristics ---
    feedback_lower = feedback.lower()
    candidates: list[tuple[Stage, str]] = []

    if stage == Stage.ANALYSIS:
        # Check claim_checklist completion via state_manager if available
        completion = None
        if state_manager and project_id:
            from .prechecks import get_completion_percentage
            completion = get_completion_percentage(state, state_manager, project_id)

        falsify_signals = ["falsif", "disproven", "hypothesis is wrong",
                           "fundamental flaw in hypothesis"]
        if any(s in feedback_lower for s in falsify_signals):
            candidates.append((Stage.HYPOTHESIS_FORMATION, "hypothesis_falsified"))
        elif completion is not None and completion < 50:
            candidates.append((Stage.EXPERIMENTATION, "need_more_experiments"))
        else:
            # Default: try more experiments
            candidates.append((Stage.EXPERIMENTATION, "need_more_experiments"))

    elif stage == Stage.EXPERIMENTATION:
        bug_signals = ["code bug", "implementation error", "runtime error",
                       "crash", "exception", "traceback"]
        if any(s in feedback_lower for s in bug_signals):
            candidates.append((Stage.IMPLEMENTATION, "code_bug_found"))

    elif stage == Stage.IMPLEMENTATION:
        design_signals = ["design flaw", "spec incomplete", "spec is wrong",
                          "infeasible", "cannot implement"]
        if any(s in feedback_lower for s in design_signals):
            candidates.append((Stage.EXPERIMENT_DESIGN, "design_flaw_found"))

    elif stage == Stage.EXPERIMENT_DESIGN:
        hyp_signals = ["hypothesis needs revision", "hypothesis too vague",
                       "hypothesis not testable"]
        if any(s in feedback_lower for s in hyp_signals):
            candidates.append((Stage.HYPOTHESIS_FORMATION, "hypothesis_needs_revision"))

    # Filter: only allowed transitions, and target not exhausted
    for target, _trigger in candidates:
        if _is_rollback_allowed(state, stage, target, max_iterations):
            return target

    return None


def _is_rollback_allowed(
    state: ProjectState, from_stage: Stage, to_stage: Stage, max_iterations: int,
) -> bool:
    """Check if a backward transition is valid and the target stage is not exhausted."""
    if (from_stage, to_stage) not in ALLOWED_TRANSITIONS:
        return False
    target_iters = state.iteration_count.get(to_stage.value, 1)
    return target_iters < max_iterations
