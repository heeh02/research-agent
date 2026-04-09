#!/usr/bin/env python3
"""Codex Critic Bridge — uses codex-plugin-cc (codex exec) for independent review.

Replaces the old gpt_review.py that called OpenAI API directly.
Advantages over raw API:
  - Codex reads the full codebase in sandbox (no context truncation)
  - Same auth as ChatGPT (no separate API key)
  - Built-in adversarial review mode
  - Structured non-interactive output

Usage (called by Claude Code via Bash, or in CI):
    python scripts/codex_review.py                           # Review current stage
    python scripts/codex_review.py --stage hypothesis_formation
    python scripts/codex_review.py --model gpt-4.1 --effort high
    python scripts/codex_review.py --interactive             # Print /codex: command instead
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research_agent.models import (
    STAGE_REQUIRED_ARTIFACTS,
    STAGE_ORDER,
    AgentRole,
    ArtifactType,
    CostRecord,
    GateCheck,
    GateResult,
    GateStatus,
    LLMProvider,
    Stage,
    resolve_critic_role,
)
from research_agent.state import StateManager
from research_agent.agents.critic import CriticAgent, STAGE_REVIEW_CRITERIA, RESEARCH_REVIEW_CRITERIA, CODE_REVIEW_CRITERIA
from research_agent.integrations.codex import (
    check_codex_available,
    codex_review,
    CodexReviewResult,
)


def load_active_project(base_dir: Path) -> tuple[StateManager, str]:
    sm = StateManager(base_dir)
    active_file = base_dir / ".active_project"
    if not active_file.exists():
        print("ERROR: No active project. Run: python scripts/pipeline.py init <name>")
        sys.exit(1)
    return sm, active_file.read_text().strip()


def collect_artifact_content(sm: StateManager, project_id: str, stage: Stage) -> str:
    """Collect all relevant artifact content for review."""
    state = sm.load_project(project_id)
    parts = [
        f"# Project: {state.name}",
        f"# Research Question: {state.research_question}",
        f"# Stage: {stage.value}",
        f"# Iteration: {state.iteration_count.get(stage.value, 1)}",
        "",
    ]

    # Collect artifacts from current and all previous stages for context
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

    # Add previous review feedback for iteration context
    prev_reviews = [g for g in state.gate_results if g.stage == stage]
    if prev_reviews:
        latest = prev_reviews[-1]
        parts.append(f"\n## Previous Review (iteration {latest.iteration})")
        parts.append(f"Status: {latest.status.value}")
        parts.append(latest.overall_feedback)

    return "\n".join(parts)


def save_review_to_state(
    sm: StateManager,
    project_id: str,
    stage: Stage,
    result: CodexReviewResult,
    model: str,
):
    """Save review result to project state and as artifact file."""
    state = sm.load_project(project_id)
    iteration = state.iteration_count.get(stage.value, 1)

    # Build gate checks from scores
    checks = []
    for criterion, score in result.scores.items():
        checks.append(GateCheck(
            name=criterion,
            description=criterion,
            check_type="codex",
            passed=score >= 0.7,
            score=score,
            feedback="",
        ))

    if result.blocking_issues:
        checks.append(GateCheck(
            name="blocking_issues",
            description="Blocking issues found by Codex",
            check_type="codex",
            passed=False,
            feedback="; ".join(result.blocking_issues),
        ))

    avg_score = sum(result.scores.values()) / len(result.scores) if result.scores else 0.0
    passed = result.verdict == "PASS" and avg_score >= 0.7
    status = GateStatus.PASSED if passed else GateStatus.FAILED

    # Build feedback summary
    feedback_parts = [f"Codex Critic Verdict: {result.verdict} (avg: {avg_score:.2f})"]
    if result.scores:
        for k, v in result.scores.items():
            icon = "✓" if v >= 0.7 else "✗"
            feedback_parts.append(f"  {icon} {k}: {v}")
    if result.blocking_issues:
        feedback_parts.append("Blocking issues:")
        for b in result.blocking_issues:
            feedback_parts.append(f"  - {b}")
    if result.strongest_objection:
        feedback_parts.append(f"Strongest objection: {result.strongest_objection}")
    if result.what_would_make_it_pass:
        feedback_parts.append(f"To pass: {result.what_would_make_it_pass}")

    gate_result = GateResult(
        gate_name=f"{stage.value}_codex_review",
        stage=stage,
        status=status,
        checks=checks,
        reviewer=resolve_critic_role(stage),
        overall_feedback="\n".join(feedback_parts),
        iteration=iteration,
    )
    state.gate_results.append(gate_result)

    # Record cost (approximate — Codex uses ChatGPT subscription)
    state.cost_records.append(CostRecord(
        agent=resolve_critic_role(stage),
        provider=LLMProvider.CODEX,
        model=model,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.05,  # Approximate per-review cost
        task_description=f"Codex review for {stage.value}",
        stage=stage,
    ))

    sm.save_project(state)

    # Save review as artifact file
    review_data = {
        "verdict": result.verdict,
        "scores": result.scores,
        "blocking_issues": result.blocking_issues,
        "suggestions": result.suggestions,
        "strongest_objection": result.strongest_objection,
        "what_would_make_it_pass": result.what_would_make_it_pass,
    }
    review_filename = f"review_report_v{iteration}.yaml"
    sm.save_artifact_file(
        project_id, stage, review_filename,
        yaml.dump(review_data, default_flow_style=False, allow_unicode=True),
    )

    return gate_result


def main():
    parser = argparse.ArgumentParser(description="Codex Critic — independent review via codex-plugin-cc")
    parser.add_argument("--stage", "-s", help="Stage to review (default: current)")
    parser.add_argument("--model", "-m", default="gpt-5.4", help="Codex model (default: gpt-5.4)")
    parser.add_argument("--effort", "-e", default="xhigh",
                        choices=["none", "low", "medium", "high", "xhigh"],
                        help="Reasoning effort (default: xhigh)")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="Print /codex: command for interactive use instead of running codex exec")
    parser.add_argument("--base-dir", default=str(ROOT))
    args = parser.parse_args()

    base_dir = Path(args.base_dir)

    # Load project
    sm, project_id = load_active_project(base_dir)
    state = sm.load_project(project_id)
    stage = Stage(args.stage) if args.stage else state.current_stage

    # Use configured critic model/effort if not overridden via CLI.
    # This script always runs via Codex CLI, so only inherit config when
    # the configured backend is also Codex (avoid passing Claude model IDs).
    config_file = base_dir / "config" / "settings.yaml"
    if config_file.exists():
        config = yaml.safe_load(config_file.read_text()) or {}
    else:
        config = {}
    critic_role = resolve_critic_role(stage)
    agent_cfg = config.get("agents", {})
    critic_cfg = agent_cfg.get(critic_role.value, agent_cfg.get("critic", {}))
    cfg_backend = critic_cfg.get("backend", "codex")
    if cfg_backend == "codex":
        # Safe to inherit model/effort from config (both are Codex-compatible)
        if args.model == "gpt-5.4":
            args.model = critic_cfg.get("model", "gpt-5.4")
        if args.effort == "xhigh":
            args.effort = critic_cfg.get("effort", "xhigh")

    critic_label = "Research Critic" if critic_role == AgentRole.RESEARCH_CRITIC else "Code Critic"
    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  {critic_label + ' Review':<43s} ║")
    print(f"║  Project: {state.name[:34]:<34s} ║")
    print(f"║  Stage:   {stage.value:<34s} ║")
    print(f"║  Model:   {args.model:<34s} ║")
    print(f"║  Effort:  {args.effort:<34s} ║")
    print(f"╚══════════════════════════════════════════════╝")
    print()

    # Interactive mode: just print the /codex: command
    if args.interactive:
        critic = CriticAgent(model=args.model, effort=args.effort)
        if stage.value in ("implementation", "experimentation"):
            cmd = critic.interactive_review_command(stage.value)
        else:
            cmd = critic.interactive_review_command(stage.value)
        print(f"Run this in your Claude Code session:\n")
        print(f"  {cmd}")
        print(f"\nOr for background review:")
        cmd_bg = critic.interactive_review_command(stage.value, background=True)
        print(f"  {cmd_bg}")
        print(f"\nThen check: /codex:status")
        print(f"And view:   /codex:result")
        return

    # Check Codex availability
    available, msg = check_codex_available()
    if not available:
        print(f"⚠  {msg}")
        print()
        # Check fallback config
        config_file = base_dir / "config" / "settings.yaml"
        if config_file.exists():
            config = yaml.safe_load(config_file.read_text()) or {}
            if config.get("codex", {}).get("fallback_to_api", False):
                print("Falling back to OpenAI API (gpt_review.py)...")
                import subprocess
                fallback_model = config.get("codex", {}).get("fallback_model", "gpt-4o")
                subprocess.run([
                    sys.executable, str(base_dir / "scripts" / "gpt_review.py"),
                    "--stage", stage.value,
                    "--model", fallback_model,
                ], cwd=base_dir)
                return
        print("Install Codex or enable fallback_to_api in config/settings.yaml")
        sys.exit(1)

    # Collect artifacts
    print("Collecting artifacts for review...")
    artifact_content = collect_artifact_content(sm, project_id, stage)

    if len(artifact_content.strip().split("\n")) < 5:
        print("⚠  Very little content to review. Make sure artifacts exist.")

    # Run Codex review
    if critic_role == AgentRole.RESEARCH_CRITIC:
        criteria = RESEARCH_REVIEW_CRITERIA.get(stage.value, "Review for scientific rigor.")
    else:
        criteria = CODE_REVIEW_CRITERIA.get(stage.value, "Review for implementation quality.")
    project_context = (
        f"Project: {state.name}\n"
        f"Research Question: {state.research_question}"
    )

    print(f"Running Codex review (model: {args.model}, effort: {args.effort})...")
    print("This may take 1-3 minutes...\n")

    result = codex_review(
        stage=stage.value,
        artifact_content=artifact_content,
        review_criteria=criteria,
        project_context=project_context,
        model=args.model,
        effort=args.effort,
        project_dir=base_dir,
    )

    # Save results
    gate_result = save_review_to_state(sm, project_id, stage, result, args.model)

    # Display
    verdict = result.verdict
    color_map = {"PASS": "\033[92m", "FAIL": "\033[91m", "REVISE": "\033[93m"}
    reset = "\033[0m"
    color = color_map.get(verdict, "")

    print(f"{'='*50}")
    print(f"Verdict: {color}{verdict}{reset}")
    print(f"{'='*50}")
    print()
    print(gate_result.overall_feedback)
    print()

    if result.suggestions:
        print("Suggestions:")
        for s in result.suggestions:
            print(f"  • {s}")
        print()

    if verdict == "PASS":
        print("✓ Gate PASSED. You may advance:")
        print("  python scripts/pipeline.py advance")
    elif verdict == "REVISE":
        print("△ REVISION needed. Address feedback, update artifact, then:")
        print("  python scripts/codex_review.py")
    else:
        print("✗ Gate FAILED. Major issues found.")
        print("  Consider: python scripts/pipeline.py rollback <stage>")

    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
