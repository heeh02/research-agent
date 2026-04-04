#!/usr/bin/env python3
"""GPT API Fallback — DEPRECATED in favor of codex_review.py (codex-plugin-cc).

This script is kept as a fallback when Codex CLI is not available.
Prefer: python scripts/codex_review.py  or  /codex:adversarial-review

Usage (only when Codex is unavailable):
    python scripts/gpt_review.py                          # Review all current stage artifacts
    python scripts/gpt_review.py --stage hypothesis_formation
    python scripts/gpt_review.py --artifact path/to/file.yaml
    python scripts/gpt_review.py --stage implementation --model gpt-4.1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research_agent.models import (
    STAGE_REQUIRED_ARTIFACTS,
    ArtifactType,
    GateCheck,
    GateResult,
    GateStatus,
    Stage,
    AgentRole,
    CostRecord,
    LLMProvider,
)
from research_agent.state import StateManager
from research_agent.integrations.llm import LLMClient, estimate_cost


# ---------------------------------------------------------------------------
# Critic prompts — stage-specific review criteria
# ---------------------------------------------------------------------------

CRITIC_SYSTEM = """\
You are a rigorous scientific reviewer (Critic Agent). Your job is to find real flaws, \
challenge assumptions, and ensure quality. You are adversarial but constructive.

RULES:
- Score each criterion 0.0-1.0 with justification
- Distinguish blocking issues (must fix) from suggestions (nice to have)
- NEVER rewrite content — only critique and suggest directions
- When rejecting, explain exactly what would make it pass
- Output ONLY valid YAML wrapped in ```yaml ... ``` fences
"""

STAGE_CRITERIA: dict[str, str] = {
    "problem_definition": """\
Review the problem_brief artifact against these criteria:
1. clarity (0-1): Problem statement unambiguous?
2. significance (0-1): Addresses a real gap?
3. scope (0-1): Neither too broad nor too narrow?
4. novelty (0-1): Genuinely different from existing work?
5. feasibility (0-1): Can be investigated with available resources?
6. references (0-1): Sufficient and relevant references?

Output YAML:
```yaml
verdict: PASS|FAIL|REVISE
scores:
  clarity: 0.0
  significance: 0.0
  scope: 0.0
  novelty: 0.0
  feasibility: 0.0
  references: 0.0
blocking_issues: []
suggestions: []
strongest_objection: ""
what_would_make_it_pass: ""
```""",

    "literature_review": """\
Review the literature_map artifact:
1. coverage (0-1): Key papers included?
2. recency (0-1): Papers from last 2 years?
3. balance (0-1): Conflicting viewpoints represented?
4. gap_identification (0-1): Gaps real and well-supported?
5. baseline_completeness (0-1): Appropriate baselines recommended?

Output YAML with verdict, scores, missing_papers, blocking_issues, suggestions, \
strongest_objection, what_would_make_it_pass.""",

    "hypothesis_formation": """\
Review the hypothesis_card artifact:
1. falsifiability (0-1): Can be disproven by experiment?
2. novelty (0-1): Not a minor variation of existing work?
3. grounding (0-1): Well-supported by literature?
4. testability (0-1): Minimum viable experiment can test this?
5. risk_awareness (0-1): Key risks identified and mitigated?
6. kill_criteria (0-1): Specific enough to act on?

Output YAML with verdict, scores, blocking_issues, suggestions, \
strongest_objection, prior_work_overlap, confounding_variables, what_would_make_it_pass.""",

    "experiment_design": """\
Review the experiment_spec artifact:
1. completeness (0-1): All variables, controls, metrics defined?
2. reproducibility (0-1): Someone else could run this from the spec?
3. statistical_rigor (0-1): Appropriate tests, sample sizes?
4. baseline_fairness (0-1): Baselines given a fair chance?
5. ablation_coverage (0-1): Ablations isolate claimed contribution?
6. budget_realism (0-1): Compute/data budget realistic?
7. failure_plan (0-1): Clear plan for when things go wrong?

Output YAML with verdict, scores, blocking_issues, missing_baselines, \
metric_concerns, suggestions, what_would_make_it_pass.""",

    "implementation": """\
Review the code and run_manifest artifacts:
1. correctness (0-1): Code implements the spec?
2. reproducibility (0-1): Seeds, versions, configs fixed?
3. test_coverage (0-1): Tests for critical paths?
4. efficiency (0-1): No obvious performance issues?
5. documentation (0-1): Can understand and run this?

Output YAML with verdict, scores, bugs_found, blocking_issues, suggestions, \
what_would_make_it_pass.""",

    "experimentation": """\
Review the run_manifest and metrics artifacts:
1. execution_complete (0-1): All planned experiments ran?
2. metrics_logged (0-1): All metrics tracked and saved?
3. reproducibility_check (0-1): Results reproducible across seeds?
4. no_anomalies (0-1): No unexplained anomalies?
5. resource_tracking (0-1): Compute usage logged?

Output YAML with verdict, scores, blocking_issues, anomalies_found, \
suggestions, what_would_make_it_pass.""",

    "analysis": """\
Review the result_report and claim_checklist:
1. evidence_support (0-1): Results support claims?
2. statistical_validity (0-1): Statistics correct?
3. alternative_explanations (0-1): Confounds addressed?
4. honest_reporting (0-1): Negative results included?
5. reproducibility (0-1): Results reproducible?

Ask: "Do results truly support the claim?" and "Is there a simpler counter-explanation?"

Output YAML with verdict, scores, unsupported_claims, blocking_issues, \
alternative_explanations, suggestions, what_would_make_it_pass.""",
}


# ---------------------------------------------------------------------------
# Core review logic
# ---------------------------------------------------------------------------

def load_active_project(base_dir: Path) -> tuple[StateManager, str]:
    sm = StateManager(base_dir)
    active_file = base_dir / ".active_project"
    if not active_file.exists():
        print("ERROR: No active project. Run: python scripts/pipeline.py init <name>")
        sys.exit(1)
    project_id = active_file.read_text().strip()
    return sm, project_id


def collect_artifacts(sm: StateManager, project_id: str, stage: Stage,
                      specific_artifact: str | None = None) -> str:
    """Collect all relevant artifact content for review."""
    state = sm.load_project(project_id)
    parts = [
        f"# Project: {state.name}",
        f"# Research Question: {state.research_question}",
        f"# Stage: {stage.value}",
        f"# Iteration: {state.iteration_count.get(stage.value, 1)}",
        "",
    ]

    if specific_artifact:
        content = Path(specific_artifact).read_text()
        parts.append(f"## Artifact: {Path(specific_artifact).name}")
        parts.append(content)
    else:
        # Collect all artifacts for this stage
        artifacts = state.stage_artifacts(stage)
        if not artifacts:
            # Also check previous stages for context
            from research_agent.models import STAGE_ORDER
            stage_idx = STAGE_ORDER.index(stage)
            for s in STAGE_ORDER[:stage_idx + 1]:
                for a in state.stage_artifacts(s):
                    try:
                        content = sm.read_artifact_file(project_id, a)
                        parts.append(f"## {a.artifact_type.value} (v{a.version}, stage: {a.stage.value})")
                        parts.append(content)
                        parts.append("")
                    except FileNotFoundError:
                        pass
        else:
            for a in artifacts:
                try:
                    content = sm.read_artifact_file(project_id, a)
                    parts.append(f"## {a.artifact_type.value} (v{a.version})")
                    parts.append(content)
                    parts.append("")
                except FileNotFoundError:
                    parts.append(f"## {a.artifact_type.value} — FILE NOT FOUND: {a.path}")

    # Add previous review feedback for context
    prev_reviews = [g for g in state.gate_results if g.stage == stage]
    if prev_reviews:
        latest = prev_reviews[-1]
        parts.append(f"\n## Previous Review (iteration {latest.iteration})")
        parts.append(f"Status: {latest.status.value}")
        parts.append(latest.overall_feedback)

    return "\n".join(parts)


def call_gpt_review(
    artifact_content: str,
    stage: str,
    model: str = "gpt-4o",
) -> tuple[dict, float]:
    """Call GPT to review the artifacts. Returns (review_dict, cost_usd)."""
    criteria = STAGE_CRITERIA.get(stage, STAGE_CRITERIA["problem_definition"])

    llm = LLMClient()
    response = llm.call(
        provider=LLMProvider.OPENAI,
        model=model,
        system_prompt=CRITIC_SYSTEM,
        user_prompt=f"{criteria}\n\n---\n\n{artifact_content}",
        max_tokens=4096,
        temperature=0.3,
    )

    # Parse YAML from response
    import re
    yaml_match = re.search(r"```ya?ml\s*\n(.*?)```", response.content, re.DOTALL)
    if yaml_match:
        try:
            review = yaml.safe_load(yaml_match.group(1))
        except yaml.YAMLError:
            review = {"verdict": "REVISE", "feedback": response.content}
    else:
        # Try to detect verdict
        upper = response.content.upper()
        if "VERDICT: PASS" in upper:
            verdict = "PASS"
        elif "VERDICT: FAIL" in upper:
            verdict = "FAIL"
        else:
            verdict = "REVISE"
        review = {"verdict": verdict, "feedback": response.content}

    return review, response.cost_usd


def save_review(
    sm: StateManager,
    project_id: str,
    stage: Stage,
    review: dict,
    cost_usd: float,
    model: str,
):
    """Save the review result to project state and as an artifact."""
    state = sm.load_project(project_id)
    iteration = state.iteration_count.get(stage.value, 1)

    # Build gate checks from scores
    checks = []
    scores = review.get("scores", {})
    for criterion, score in scores.items():
        checks.append(GateCheck(
            name=criterion,
            description=criterion,
            check_type="ai_eval",
            passed=score >= 0.7 if isinstance(score, (int, float)) else False,
            score=float(score) if isinstance(score, (int, float)) else 0.0,
            feedback="",
        ))

    # Check for blocking issues
    blocking = review.get("blocking_issues", [])
    if blocking:
        checks.append(GateCheck(
            name="blocking_issues",
            description="Blocking issues found by reviewer",
            check_type="ai_eval",
            passed=False,
            feedback="; ".join(str(b) for b in blocking),
        ))

    verdict = review.get("verdict", "REVISE").upper()
    avg_score = sum(scores.values()) / len(scores) if scores else 0.0
    passed = verdict == "PASS" and avg_score >= 0.7

    status = GateStatus.PASSED if passed else GateStatus.FAILED

    # Build feedback
    feedback_parts = [f"GPT Critic Verdict: {verdict} (avg: {avg_score:.2f})"]
    if scores:
        for k, v in scores.items():
            icon = "✓" if (isinstance(v, (int, float)) and v >= 0.7) else "✗"
            feedback_parts.append(f"  {icon} {k}: {v}")
    if blocking:
        feedback_parts.append("Blocking issues:")
        for b in blocking:
            feedback_parts.append(f"  - {b}")
    objection = review.get("strongest_objection", "")
    if objection:
        feedback_parts.append(f"Strongest objection: {objection}")
    fix = review.get("what_would_make_it_pass", "")
    if fix:
        feedback_parts.append(f"To pass: {fix}")

    gate_result = GateResult(
        gate_name=f"{stage.value}_gpt_review",
        stage=stage,
        status=status,
        checks=checks,
        reviewer=AgentRole.CRITIC,
        overall_feedback="\n".join(feedback_parts),
        iteration=iteration,
    )
    state.gate_results.append(gate_result)

    # Record cost
    state.cost_records.append(CostRecord(
        agent=AgentRole.CRITIC,
        provider=LLMProvider.OPENAI,
        model=model,
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost_usd,
        task_description=f"GPT review for {stage.value}",
        stage=stage,
    ))

    sm.save_project(state)

    # Also save review as artifact file
    review_filename = f"review_report_v{iteration}.yaml"
    review_content = yaml.dump(review, default_flow_style=False, allow_unicode=True)
    sm.save_artifact_file(project_id, stage, review_filename, review_content)

    return gate_result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GPT Critic — independent artifact review")
    parser.add_argument("--stage", "-s", help="Stage to review (default: current)")
    parser.add_argument("--artifact", "-a", help="Specific artifact file to review")
    parser.add_argument("--model", "-m", default="gpt-4o", help="GPT model (default: gpt-4o)")
    parser.add_argument("--base-dir", default=str(ROOT), help="Project base directory")
    args = parser.parse_args()

    base_dir = Path(args.base_dir)

    # Check API key
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set.")
        print("Run: export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    # Load project
    sm, project_id = load_active_project(base_dir)
    state = sm.load_project(project_id)

    stage = Stage(args.stage) if args.stage else state.current_stage

    print(f"╔══════════════════════════════════════════╗")
    print(f"║  GPT Critic Review                       ║")
    print(f"║  Project: {state.name[:30]:<30s} ║")
    print(f"║  Stage:   {stage.value:<30s} ║")
    print(f"║  Model:   {args.model:<30s} ║")
    print(f"╚══════════════════════════════════════════╝")
    print()

    # Collect artifacts for review
    print("Collecting artifacts for review...")
    artifact_content = collect_artifacts(sm, project_id, stage, args.artifact)

    if len(artifact_content.strip().split("\n")) < 5:
        print("WARNING: Very little content to review. Make sure artifacts exist.")

    # Call GPT
    print(f"Calling {args.model} for review...")
    review, cost = call_gpt_review(artifact_content, stage.value, args.model)

    # Save results
    gate_result = save_review(sm, project_id, stage, review, cost, args.model)

    # Display results
    verdict = review.get("verdict", "UNKNOWN")
    color_map = {"PASS": "\033[92m", "FAIL": "\033[91m", "REVISE": "\033[93m"}
    reset = "\033[0m"
    color = color_map.get(verdict, "")

    print()
    print(f"{'='*50}")
    print(f"Verdict: {color}{verdict}{reset}")
    print(f"{'='*50}")
    print()
    print(gate_result.overall_feedback)
    print()
    print(f"Cost: ${cost:.4f}")
    print()

    if verdict == "PASS":
        print("✓ Gate PASSED. You may advance to the next stage.")
        print("  Run: python scripts/pipeline.py advance")
    elif verdict == "REVISE":
        print("△ REVISION needed. Address the feedback above, update your artifact,")
        print("  then re-run: python scripts/gpt_review.py")
    else:
        print("✗ Gate FAILED. Major issues found. Consider rollback if needed.")
        print(f"  Rollback: python scripts/pipeline.py rollback <stage>")

    # Return exit code based on verdict
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
