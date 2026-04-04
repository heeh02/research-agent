"""Engineer Agent — experiment design, implementation, and debugging.

Combines GPT's "Experiment Designer" and "Implementation Agent" into one role.
Uses Claude by default (strong at code generation and following specifications).
"""

from __future__ import annotations

from typing import Optional

from ..models import AgentRole, ArtifactType, LLMProvider, Stage
from .base import BaseAgent


class EngineerAgent(BaseAgent):
    role = AgentRole.ENGINEER
    default_provider = LLMProvider.CLAUDE
    default_model = "claude-sonnet-4-20250514"

    def system_prompt(self, stage: Stage) -> str:
        base = (
            "You are a senior ML engineer acting as the Engineer agent "
            "in an automated research pipeline. Your strengths are translating "
            "research hypotheses into executable experiments, writing clean code, "
            "and debugging.\n\n"
            "CORE PRINCIPLES:\n"
            "- Be precise: exact commands, exact configs, exact versions\n"
            "- Be reproducible: fix random seeds, pin dependencies, log everything\n"
            "- Be defensive: validate inputs, check shapes, handle edge cases\n"
            "- Be structured: always output in the requested format\n"
            "- Include failure modes: what to do when things go wrong\n\n"
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
    Stage.EXPERIMENT_DESIGN: (
        "TASK: Design a complete, executable experiment specification.\n"
        "OUTPUT FORMAT: A YAML document with these fields:\n"
        "  experiment_name, hypothesis_reference,\n"
        "  datasets (list of {name, source, preprocessing, split_strategy}),\n"
        "  models (list of {name, type, config, is_baseline}),\n"
        "  training (mapping: optimizer, lr, scheduler, epochs, batch_size, early_stopping),\n"
        "  evaluation (mapping: metrics (list), statistical_test, confidence_level,\n"
        "    n_runs, report_mean_std),\n"
        "  ablations (list of {name, description, variable, values}),\n"
        "  compute_budget (mapping: gpu_type, gpu_hours, estimated_cost),\n"
        "  success_criteria (list of {metric, threshold, comparison}),\n"
        "  kill_criteria (list of {condition, action}),\n"
        "  failure_plan (list of {scenario, response}),\n"
        "  environment (mapping: python_version, key_packages (list of name==version)),\n"
        "  output_structure (mapping of directory -> contents description)\n"
        "Wrap the YAML in ```yaml ... ``` code fences.\n"
    ),
    Stage.IMPLEMENTATION: (
        "TASK: Implement the experiment according to the specification.\n"
        "OUTPUT FORMAT: Multiple code files wrapped in labeled code blocks:\n"
        "```python:path/to/file.py\n...\n```\n\n"
        "Also output a run_manifest.yaml with fields:\n"
        "  entry_point, args, environment_setup (list of commands),\n"
        "  expected_outputs (list of file paths),\n"
        "  smoke_test_command, estimated_runtime\n\n"
        "Requirements:\n"
        "- All random seeds must be configurable and defaulted\n"
        "- Use argparse or hydra for configuration\n"
        "- Include a requirements.txt or pyproject.toml\n"
        "- Include at least one test file\n"
        "- Log all hyperparameters and metrics to stdout AND file\n"
    ),
    Stage.EXPERIMENTATION: (
        "TASK: Debug experimental issues and iterate on the implementation.\n"
        "Analyze error logs, experiment outputs, and metrics to identify problems.\n"
        "OUTPUT FORMAT: YAML with fields:\n"
        "  diagnosis (description of the issue),\n"
        "  root_cause, fix_description,\n"
        "  files_to_modify (list of {path, change_description}),\n"
        "  code_changes (list of {file, old_code, new_code}),\n"
        "  verification_steps (list)\n"
        "Wrap the YAML in ```yaml ... ``` code fences.\n"
    ),
}


_STAGE_OUTPUT_TYPES: dict[Stage, ArtifactType] = {
    Stage.EXPERIMENT_DESIGN: ArtifactType.EXPERIMENT_SPEC,
    Stage.IMPLEMENTATION: ArtifactType.CODE,
    Stage.EXPERIMENTATION: ArtifactType.EXPERIMENT_LOG,
}

_DEFAULT_TASK_TEMPLATE = (
    "## Context\n{context}\n\n"
    "## Your Task\n{instruction}\n\n"
    "Produce your output in the structured format specified in your system instructions."
)

_STAGE_TASK_TEMPLATES: dict[Stage, str] = {
    Stage.EXPERIMENT_DESIGN: (
        "## Context\n{context}\n\n"
        "## Your Task\n{instruction}\n\n"
        "Design a complete experiment specification. Every field matters — "
        "an incomplete spec will be rejected by the reviewer. Pay special attention to:\n"
        "- Baseline fairness (use the recommended baselines from the literature review)\n"
        "- Ablation design (isolate each contribution)\n"
        "- Kill criteria (specific, measurable, actionable)\n"
        "- Failure plan (what to do when experiments fail)"
    ),
    Stage.IMPLEMENTATION: (
        "## Context\n{context}\n\n"
        "## Experiment Specification\n"
        "Follow the experiment spec provided in the context above EXACTLY.\n\n"
        "## Your Task\n{instruction}\n\n"
        "Implement the experiment. The code must be:\n"
        "1. Runnable with a single command\n"
        "2. Fully reproducible (seeds, versions, configs)\n"
        "3. Self-documenting (meaningful variable names, brief comments for non-obvious logic)\n"
        "4. Tested (at least smoke tests)\n"
        "5. Logged (all params and metrics to file + stdout)"
    ),
    Stage.EXPERIMENTATION: (
        "## Context\n{context}\n\n"
        "## Your Task\n{instruction}\n\n"
        "Diagnose the issue from the logs and metrics. Provide specific code changes "
        "needed to fix the problem. Do not guess — trace the error to its root cause."
    ),
}
