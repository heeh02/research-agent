"""Researcher Agent — literature review, synthesis, hypothesis formation.

Combines GPT's "Research Scout" and "Research Synthesizer" into one role.
Uses Claude by default (strong synthesis and long-context ability).
"""

from __future__ import annotations

from typing import Optional

from ..models import AgentRole, ArtifactType, LLMProvider, Stage
from .base import BaseAgent


class ResearcherAgent(BaseAgent):
    role = AgentRole.RESEARCHER
    default_provider = LLMProvider.CLAUDE
    default_model = "claude-sonnet-4-20250514"

    def system_prompt(self, stage: Stage) -> str:
        base = (
            "You are a senior research scientist acting as the Researcher agent "
            "in an automated research pipeline. Your strengths are literature analysis, "
            "synthesis, gap identification, and hypothesis formation.\n\n"
            "CORE PRINCIPLES:\n"
            "- Be evidence-based: cite specific papers, methods, and results\n"
            "- Be precise: use quantitative claims where possible\n"
            "- Be honest: clearly distinguish established facts from speculation\n"
            "- Be structured: always output in the requested YAML/markdown format\n"
            "- Be concise: no filler, no generic statements\n\n"
        )
        stage_specific = _STAGE_SYSTEM_PROMPTS.get(stage, "")
        return base + stage_specific

    def task_prompt(self, stage: Stage, context: str, instruction: str) -> str:
        template = _STAGE_TASK_TEMPLATES.get(stage, _DEFAULT_TASK_TEMPLATE)
        return template.format(context=context, instruction=instruction)

    def expected_output_type(self, stage: Stage) -> Optional[ArtifactType]:
        return _STAGE_OUTPUT_TYPES.get(stage)


# ---------------------------------------------------------------------------
# Stage-specific prompts
# ---------------------------------------------------------------------------

_STAGE_SYSTEM_PROMPTS: dict[Stage, str] = {
    Stage.PROBLEM_DEFINITION: (
        "TASK: Define a research problem.\n"
        "OUTPUT FORMAT: A YAML document with these fields:\n"
        "  domain, problem_statement, motivation, scope, existing_approaches (list),\n"
        "  limitations_of_existing (list), proposed_direction, success_criteria,\n"
        "  key_references (list of {title, authors, year, relevance})\n"
        "Wrap the YAML in ```yaml ... ``` code fences.\n"
    ),
    Stage.LITERATURE_REVIEW: (
        "TASK: Conduct a thorough literature review.\n"
        "OUTPUT FORMAT: A YAML document with these fields:\n"
        "  research_question, search_scope, papers (list of {title, authors, year, venue,\n"
        "    method, key_results, limitations, relevance_score}),\n"
        "  method_taxonomy (mapping of category -> list of methods),\n"
        "  identified_gaps (list), conflicting_findings (list),\n"
        "  trend_analysis, recommended_baselines (list)\n"
        "Wrap the YAML in ```yaml ... ``` code fences.\n"
    ),
    Stage.HYPOTHESIS_FORMATION: (
        "TASK: Formulate a testable research hypothesis.\n"
        "OUTPUT FORMAT: A YAML document (hypothesis card) with these fields:\n"
        "  claim, motivation, why_now, novelty_argument,\n"
        "  key_assumptions (list), testable_predictions (list),\n"
        "  baseline_comparison, expected_improvement,\n"
        "  key_risks (list of {risk, likelihood, mitigation}),\n"
        "  kill_criteria (list — conditions under which we abandon this hypothesis),\n"
        "  minimum_viable_experiment, estimated_compute_budget\n"
        "Wrap the YAML in ```yaml ... ``` code fences.\n"
    ),
    Stage.ANALYSIS: (
        "TASK: Analyze experimental results and draw conclusions.\n"
        "OUTPUT FORMAT: A YAML document with these fields:\n"
        "  hypothesis_recap, experiments_run (list),\n"
        "  key_results (list of {experiment, metric, value, baseline_value, improvement}),\n"
        "  statistical_significance, ablation_findings (list),\n"
        "  claims_supported (list), claims_not_supported (list),\n"
        "  alternative_explanations (list), limitations (list),\n"
        "  future_work (list), conclusion\n"
        "Wrap the YAML in ```yaml ... ``` code fences.\n"
    ),
}


_STAGE_OUTPUT_TYPES: dict[Stage, ArtifactType] = {
    Stage.PROBLEM_DEFINITION: ArtifactType.PROBLEM_BRIEF,
    Stage.LITERATURE_REVIEW: ArtifactType.LITERATURE_MAP,
    Stage.HYPOTHESIS_FORMATION: ArtifactType.HYPOTHESIS_CARD,
    Stage.ANALYSIS: ArtifactType.RESULT_REPORT,
}

_DEFAULT_TASK_TEMPLATE = (
    "## Context\n{context}\n\n"
    "## Your Task\n{instruction}\n\n"
    "Produce your output in the structured format specified in your system instructions."
)

_STAGE_TASK_TEMPLATES: dict[Stage, str] = {
    Stage.PROBLEM_DEFINITION: (
        "## Context\n{context}\n\n"
        "## Your Task\n{instruction}\n\n"
        "Define the research problem. Be specific about what gap you're addressing "
        "and why it matters now. Include at least 5 key references."
    ),
    Stage.LITERATURE_REVIEW: (
        "## Context\n{context}\n\n"
        "## Your Task\n{instruction}\n\n"
        "Analyze the literature thoroughly. For each paper, extract the method, "
        "key results, and limitations. Identify at least 3 research gaps and "
        "any conflicting findings between papers. Recommend baselines for experiments."
    ),
    Stage.HYPOTHESIS_FORMATION: (
        "## Context\n{context}\n\n"
        "## Previous Artifacts\n"
        "Use the problem brief and literature map provided in the context above.\n\n"
        "## Your Task\n{instruction}\n\n"
        "Formulate a precise, testable hypothesis. The kill criteria are critical — "
        "define exactly when we should abandon this direction. The minimum viable "
        "experiment should be achievable within the compute budget."
    ),
    Stage.ANALYSIS: (
        "## Context\n{context}\n\n"
        "## Your Task\n{instruction}\n\n"
        "Analyze the experimental results rigorously. For every claim, cite the "
        "specific experiment and metric that supports it. Actively look for "
        "alternative explanations and confounding factors."
    ),
}
