"""Critic Agent — the adversarial reviewer that ensures quality.

Powered by OpenAI Codex via codex-plugin-cc. Two operating modes:

1. **Interactive** (in Claude Code session):
   Use /codex:adversarial-review directly — Codex reads the codebase,
   reviews in-context, returns structured feedback.

2. **Programmatic** (via scripts/pipeline):
   Uses `codex exec` for non-interactive structured review.
   This mode is used by the gate system and CI.

Why Codex instead of raw GPT API:
- Codex can read the full project (sandbox file access) — no context truncation
- Built-in adversarial review mode designed for exactly this use case
- Background jobs — reviews don't block Claude Code
- Review gate feature — auto-blocks Claude until Codex approves
- Same auth as ChatGPT — no separate API key management

The Critic NEVER produces primary content. It only reviews and challenges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from ..integrations.codex import (
    CodexReviewResult,
    codex_review,
    check_codex_available,
)
from ..models import (
    AgentRole,
    AgentMessage,
    ArtifactType,
    CostRecord,
    GateCheck,
    LLMProvider,
    ProjectState,
    Stage,
)


class CriticAgent:
    """Adversarial reviewer powered by OpenAI Codex.

    Unlike the Researcher and Engineer (which run as CLI subprocesses),
    the Critic uses the Codex CLI as its backend. This provides:
    - True independent perspective (different model family)
    - Codebase-aware reviews (Codex reads files in sandbox)
    - Structured adversarial review mode
    """

    role = AgentRole.CRITIC

    def __init__(
        self,
        model: str = "gpt-5.4",
        effort: str = "xhigh",
        project_dir: Optional[Path] = None,
    ):
        self.model = model
        self.effort = effort
        self.project_dir = project_dir

    def review(
        self,
        stage: Stage,
        artifact_content: str,
        project_context: str,
        state: ProjectState,
    ) -> tuple[CodexReviewResult, GateCheck]:
        """Run a Codex review and return (result, gate_check).

        This is the programmatic review path used by the gate system.
        For interactive use, call /codex:adversarial-review directly.
        """
        criteria = STAGE_REVIEW_CRITERIA.get(stage.value, _DEFAULT_CRITERIA)

        result = codex_review(
            stage=stage.value,
            artifact_content=artifact_content,
            review_criteria=criteria,
            project_context=project_context,
            model=self.model,
            effort=self.effort,
            project_dir=self.project_dir,
        )

        # Record message
        state.messages.append(AgentMessage(
            sender=self.role,
            receiver=AgentRole.ORCHESTRATOR,
            content=f"Codex review: {result.verdict} (scores: {result.scores})",
            stage=stage,
        ))

        # Build gate check
        avg_score = (
            sum(result.scores.values()) / len(result.scores)
            if result.scores else 0.0
        )
        passed = result.verdict == "PASS" and avg_score >= 0.7

        feedback_parts = [f"Codex Verdict: {result.verdict} (avg: {avg_score:.2f})"]
        if result.scores:
            for criterion, score in result.scores.items():
                icon = "✓" if score >= 0.7 else "✗"
                feedback_parts.append(f"  {icon} {criterion}: {score}")
        if result.blocking_issues:
            feedback_parts.append("Blocking issues:")
            for issue in result.blocking_issues:
                feedback_parts.append(f"  - {issue}")
        if result.strongest_objection:
            feedback_parts.append(f"Strongest objection: {result.strongest_objection}")
        if result.what_would_make_it_pass:
            feedback_parts.append(f"To pass: {result.what_would_make_it_pass}")

        gate_check = GateCheck(
            name="codex_review",
            description="Codex adversarial review",
            check_type="codex",
            passed=passed,
            score=avg_score,
            feedback="\n".join(feedback_parts),
        )

        return result, gate_check

    @staticmethod
    def interactive_review_command(
        stage: str,
        focus: str = "",
        background: bool = False,
        base_branch: str = "main",
    ) -> str:
        """Generate the /codex: command string for interactive use in Claude Code.

        Claude Code calls this to get the right command, then executes it.
        """
        if stage in ("implementation", "experimentation"):
            # Code-heavy stages: use standard review with diff
            cmd = f"/codex:review --base {base_branch}"
            if background:
                cmd += " --background"
            return cmd
        else:
            # Research/design stages: use adversarial review
            cmd = "/codex:adversarial-review"
            if background:
                cmd += " --background"
            if focus:
                cmd += f" {focus}"
            else:
                cmd += f" review the {stage} artifacts for scientific rigor"
            return cmd

    @staticmethod
    def rescue_command(task: str, model: str = "gpt-5.4", effort: str = "medium") -> str:
        """Generate a /codex:rescue command for delegating investigation to Codex."""
        return f"/codex:rescue --model {model} --effort {effort} {task}"


# ---------------------------------------------------------------------------
# Stage-specific review criteria (fed to Codex as part of the prompt)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Split review criteria: research vs code
# ---------------------------------------------------------------------------

RESEARCH_REVIEW_CRITERIA: dict[str, str] = {
    "problem_definition": """\
Review the problem_brief artifact against these criteria:
1. clarity (0-1): Problem statement unambiguous?
2. significance (0-1): Addresses a real gap?
3. scope (0-1): Neither too broad nor too narrow?
4. novelty (0-1): Genuinely different from existing work?
5. feasibility (0-1): Can be investigated with available resources?
6. references (0-1): Sufficient and relevant references?""",

    "literature_review": """\
Review the literature_map artifact:
1. coverage (0-1): Key papers included?
2. recency (0-1): Papers from last 2 years?
3. balance (0-1): Conflicting viewpoints represented?
4. gap_identification (0-1): Gaps real and well-supported?
5. baseline_completeness (0-1): Appropriate baselines recommended?""",

    "hypothesis_formation": """\
Review the hypothesis_card artifact:
1. falsifiability (0-1): Can be disproven by experiment?
2. novelty (0-1): Not a minor variation of existing work?
3. grounding (0-1): Well-supported by literature?
4. testability (0-1): Minimum viable experiment can test this?
5. risk_awareness (0-1): Key risks identified and mitigated?
6. kill_criteria (0-1): Specific enough to act on?""",

    "analysis": """\
Review the result_report and claim_checklist:
1. evidence_support (0-1): Results support claims?
2. statistical_validity (0-1): Statistics correct?
3. alternative_explanations (0-1): Confounds addressed?
4. honest_reporting (0-1): Negative results included?
5. reproducibility (0-1): Results reproducible?

Ask: "Do results truly support the claim?" and "Is there a simpler counter-explanation?" """,
}

CODE_REVIEW_CRITERIA: dict[str, str] = {
    "experiment_design": """\
Review the experiment_spec artifact:
1. completeness (0-1): All variables, controls, metrics defined?
2. reproducibility (0-1): Someone else could run this from the spec?
3. statistical_rigor (0-1): Appropriate tests, sample sizes?
4. baseline_fairness (0-1): Baselines given a fair chance?
5. ablation_coverage (0-1): Ablations isolate claimed contribution?
6. budget_realism (0-1): Compute/data budget realistic?
7. failure_plan (0-1): Clear plan for when things go wrong?""",

    "implementation": """\
Review the code and run_manifest artifacts:
1. correctness (0-1): Code implements the spec?
2. reproducibility (0-1): Seeds, versions, configs fixed?
3. test_coverage (0-1): Tests for critical paths?
4. efficiency (0-1): No obvious performance issues?
5. documentation (0-1): Can understand and run this?""",

    "experimentation": """\
Review the run_manifest and metrics artifacts:
1. execution_complete (0-1): All planned experiments ran?
2. metrics_logged (0-1): All metrics tracked and saved?
3. reproducibility_check (0-1): Results reproducible across seeds?
4. no_anomalies (0-1): No unexplained anomalies?
5. resource_tracking (0-1): Compute usage logged?""",
}

# Merged dict for backward compat
STAGE_REVIEW_CRITERIA: dict[str, str] = {
    **RESEARCH_REVIEW_CRITERIA,
    **CODE_REVIEW_CRITERIA,
}

_DEFAULT_CRITERIA = """\
Review the artifacts for scientific rigor:
1. completeness (0-1)
2. correctness (0-1)
3. clarity (0-1)
4. novelty (0-1)
5. feasibility (0-1)"""
