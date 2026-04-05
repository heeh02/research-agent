#!/usr/bin/env python3
"""Multi-Agent Orchestrator — dispatches to separate Claude Code instances.

Each agent runs as an independent `claude -p` process.
Version system: major.minor (stage_index.iteration).
Two modes:
  - advance/step: human confirms before each version bump (yes/no + feedback)
  - auto: fully automatic, no confirmation prompts

Usage:
    python scripts/multi_agent.py status
    python scripts/multi_agent.py step [-i instruction]
    python scripts/multi_agent.py review
    python scripts/multi_agent.py auto [--until stage] [-n max_revisions]
    python scripts/multi_agent.py timeline              # Print version timeline
    python scripts/multi_agent.py gui                   # Launch web GUI
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research_agent.models import (
    ALLOWED_TRANSITIONS,
    STAGE_ORDER,
    STAGE_PRIMARY_AGENT,
    STAGE_REQUIRED_ARTIFACTS,
    AgentRole,
    ArtifactType,
    GateCheck,
    GateResult,
    GateStatus,
    Stage,
    VersionEventType,
)
from research_agent.state import StateManager
from research_agent.artifacts import create_artifact
from research_agent.dispatcher import MultiAgentDispatcher, TaskCard, AgentResult


def load_config() -> dict:
    f = ROOT / "config" / "settings.yaml"
    return yaml.safe_load(f.read_text()) if f.exists() else {}


def get_active(sm: StateManager) -> str:
    af = ROOT / ".active_project"
    if not af.exists():
        print("No active project. Run: python scripts/pipeline.py init <name>")
        sys.exit(1)
    return af.read_text().strip()


# ---------------------------------------------------------------------------
# Human confirmation (advance/step mode only)
# ---------------------------------------------------------------------------

def confirm_version_bump(
    state, bump_type: str, description: str, sm: StateManager, project_id: str,
) -> tuple[bool, str]:
    """Prompt user for yes/no before version bump.

    Returns (approved, feedback).
    If approved=False, feedback contains user's guidance.
    """
    ver = state.current_version()
    print(f"\n{'─'*60}")
    print(f"  Version: {ver}  →  {bump_type} version bump")
    print(f"  {description}")
    print(f"{'─'*60}")
    print()

    try:
        answer = input("  Approve? [yes/no]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  (interrupted)")
        return False, ""

    if answer in ("yes", "y", ""):
        state.record_event(VersionEventType.HUMAN_APPROVE, f"Human approved {bump_type} bump")
        sm.save_project(state)
        return True, ""

    # No → get feedback
    print()
    print("  Please describe what the planning agent should change:")
    try:
        feedback = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        feedback = ""

    state.record_event(
        VersionEventType.HUMAN_REJECT,
        f"Human rejected {bump_type} bump",
        detail=feedback,
    )
    sm.save_project(state)
    return False, feedback


# ---------------------------------------------------------------------------
# Task card builder
# ---------------------------------------------------------------------------

def build_task_card(state, stage, role, project_id, instruction="", previous_feedback=""):
    task_id = f"{stage.value}-{uuid.uuid4().hex[:6]}"
    artifact_dir = f"projects/{project_id}/artifacts/{stage.value}"

    # Only include LATEST version of each artifact type (avoid confusing Critic with history)
    context_files = []
    seen_types = set()
    stage_idx = STAGE_ORDER.index(stage)
    for s in STAGE_ORDER[:stage_idx + 1]:
        for atype in STAGE_REQUIRED_ARTIFACTS.get(s, []):
            latest = state.latest_artifact(atype)
            if latest and atype.value not in seen_types:
                context_files.append(f"projects/{project_id}/{latest.path}")
                seen_types.add(atype.value)

    required_outputs = []
    for atype in STAGE_REQUIRED_ARTIFACTS.get(stage, []):
        existing = [a for a in state.artifacts if a.artifact_type == atype]
        version = max((a.version for a in existing), default=0) + 1
        required_outputs.append(f"{artifact_dir}/{atype.value}_v{version}.yaml")

    if not instruction:
        instruction = _default_instruction(stage, state)

    return TaskCard(
        task_id=task_id, role=role, stage=stage, instruction=instruction,
        context_files=context_files, required_outputs=required_outputs,
        previous_feedback=previous_feedback,
        constraints=[
            f"Project: {state.name}",
            f"Research question: {state.research_question}",
            f"Iteration: {state.iteration_count.get(stage.value, 1)}",
            f"Write output files to: {artifact_dir}/",
        ],
        metadata={"project_id": project_id, "iteration": state.iteration_count.get(stage.value, 1)},
    )


def _default_instruction(stage, state):
    q = state.research_question or "the research question"
    return {
        Stage.PROBLEM_DEFINITION: f"Define the research problem for: {q}. Include 5+ references.",
        Stage.LITERATURE_REVIEW: "Thorough literature review. Read problem_brief first. Find papers, gaps, baselines.",
        Stage.HYPOTHESIS_FORMATION: "Formulate testable hypothesis. Kill criteria are critical.",
        Stage.EXPERIMENT_DESIGN: "Design complete experiment. Read hypothesis_card. Baselines + ablations + failure plan.",
        Stage.IMPLEMENTATION: "Implement experiment per spec. Single command, reproducible, with tests.",
        Stage.EXPERIMENTATION: "Verify experiment ready. Smoke test must pass.",
        Stage.ANALYSIS: "Analyze results. Cite specific experiments for every claim.",
    }.get(stage, "Proceed.")


# ---------------------------------------------------------------------------
# Core pipeline step (with version events)
# ---------------------------------------------------------------------------

def run_step(sm, dispatcher, project_id, instruction="", force_stage=None, auto_mode=False):
    """Run one step. In non-auto mode, asks for human confirmation before minor version bump."""
    state = sm.load_project(project_id)
    stage = force_stage or state.current_stage
    role = STAGE_PRIMARY_AGENT[stage]

    # Human confirmation for minor version bump (revision) in advance/step mode
    if not auto_mode and state.iteration_count.get(stage.value, 1) > 1:
        approved, feedback = confirm_version_bump(
            state, "minor",
            f"Re-run {role.value} agent for {stage.value} (revision {state.current_iteration()})",
            sm, project_id,
        )
        if not approved:
            if feedback:
                instruction = f"HUMAN FEEDBACK: {feedback}\n\n{instruction}" if instruction else f"HUMAN FEEDBACK: {feedback}"
                state.record_event(VersionEventType.HUMAN_FEEDBACK, f"Human guidance: {feedback[:100]}", detail=feedback)
                sm.save_project(state)
            else:
                return AgentResult(task_id="skipped", role=role, success=False, output_text="Skipped by user"), "Skipped"

    state = sm.load_project(project_id)
    previous_feedback = ""
    stage_gates = [g for g in state.gate_results if g.stage == stage]
    if stage_gates and stage_gates[-1].status == GateStatus.FAILED:
        previous_feedback = stage_gates[-1].overall_feedback

    task = build_task_card(state, stage, role, project_id, instruction, previous_feedback)

    ver = state.current_version()
    print(f"┌─ v{ver} Dispatching: {role.value} agent")
    eff = dispatcher.effort.get(role, 'high')
    backend = dispatcher.backends.get(role, "claude")
    print(f"│  Stage: {stage.value} | CLI: {backend.value} | Model: {dispatcher.models.get(role, '?')} | Effort: {eff}")
    print(f"│  Tools: {dispatcher._get_toolset(role)}")
    print(f"└─ Running...")
    print()

    result = dispatcher.dispatch(task)

    # Handle auth errors — pause and let user fix
    if result.is_auth_error:
        print(f"  ✗ AUTH ERROR: API returned 403 or login required.")
        print(f"  ✗ Please run:  /login  or  claude login")
        print(f"  ✗ Then re-run the pipeline command.")
        state = sm.load_project(project_id)
        state.record_event(VersionEventType.GATE_FAILED,
            f"Auth error — pipeline paused (retried {result.retries}x)",
            agent=role, detail=result.output_text[:500])
        sm.save_project(state)
        return result, "Auth error — paused"

    icon = "✓" if result.success else "✗"
    retry_str = f" (retried {result.retries}x)" if result.retries > 0 else ""
    print(f"┌─ {icon} v{ver} Agent done ({result.duration_seconds:.1f}s){retry_str}")
    print(f"│  Files: {result.output_files}")
    print(f"└─")
    print()

    # Record event + register artifacts
    state = sm.load_project(project_id)
    state.record_event(
        VersionEventType.AGENT_RUN,
        f"{role.value} produced artifacts for {stage.value}",
        agent=role,
        artifacts_produced=result.output_files,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        detail=result.output_text,
    )

    for output_path in result.output_files:
        for atype in ArtifactType:
            if atype.value in Path(output_path).stem:
                create_artifact(state, atype, stage, role, Path(output_path).name)
                break

    sm.save_project(state)
    return result, f"v{ver} {role.value} → {stage.value}"


def run_review(sm, dispatcher, project_id, auto_mode=False):
    """Run Codex review. Records version event."""
    state = sm.load_project(project_id)
    stage = state.current_stage
    ver = state.current_version()

    from research_agent.agents.critic import STAGE_REVIEW_CRITERIA
    criteria = STAGE_REVIEW_CRITERIA.get(stage.value, "Review for scientific rigor.")

    # Build proper review instruction so non-codex critics know the expected format
    review_instr = (
        f"CRITICAL: You are a REVIEWER. Do NOT write any files. Do NOT create v2 artifacts.\n"
        f"ONLY output a review YAML block.\n\n"
        f"Review the {stage.value} artifacts.\n\n"
        f"## Review Criteria\n{criteria}\n\n"
        f"## Required Output (print YAML, do NOT write files)\n"
        f"verdict: PASS | REVISE | FAIL\n"
        f"scores: {{rigor, completeness, clarity, novelty}} each 0.0-1.0\n"
        f"blocking_issues: [list]\n"
        f"suggestions: [list]\n"
        f"strongest_objection: str\n"
        f"what_would_make_it_pass: str\n\n"
        f"PASS only if ALL scores >= 0.7 AND no blocking issues.\n"
    )
    task = build_task_card(state, stage, AgentRole.CRITIC, project_id, review_instr)

    critic_backend = dispatcher.backends.get(AgentRole.CRITIC, "codex")
    critic_model = dispatcher.models.get(AgentRole.CRITIC, "gpt-5.4")
    print(f"┌─ v{ver} Critic ({critic_backend.value}/{critic_model})")
    print(f"└─ Reviewing {stage.value}...")
    print()

    result = dispatcher.dispatch(task)

    # Handle auth/network errors from Codex
    if result.is_auth_error or (not result.success and not result.output_text.strip()):
        retry_str = f" (retried {result.retries}x)" if result.retries else ""
        print(f"  ✗ Codex review failed{retry_str}: {result.error or 'no output'}")
        print(f"  ✗ Check: codex login  or network connection")
        state = sm.load_project(project_id)
        state.record_event(VersionEventType.GATE_FAILED,
            f"Codex unavailable — review skipped{retry_str}",
            agent=AgentRole.CRITIC, detail=result.output_text[:500])
        gate_result = GateResult(
            gate_name=f"{stage.value}_codex_review", stage=stage,
            status=GateStatus.FAILED,
            checks=[GateCheck(name="codex_error", description="Codex unreachable",
                check_type="codex", passed=False, feedback=f"Network/auth error{retry_str}")],
            reviewer=AgentRole.CRITIC,
            overall_feedback=f"Codex review failed (network/auth). Re-run when connection is restored.",
            iteration=state.iteration_count.get(stage.value, 1),
        )
        state.gate_results.append(gate_result)
        sm.save_project(state)
        return result, gate_result

    verdict = "REVISE"
    upper_out = result.output_text.upper()
    if result.success:
        verdict = "PASS"
    elif "VERDICT: PASS" in upper_out or "VERDICT:PASS" in upper_out:
        verdict = "PASS"
    elif any(v in upper_out for v in ["VERDICT: FAIL", "VERDICT:FAIL", "VERDICT: REJECT", "VERDICT:REJECT"]):
        verdict = "FAIL"
    elif any(v in upper_out for v in ["VERDICT: REVISE", "VERDICT:REVISE"]):
        verdict = "REVISE"

    gate_result = GateResult(
        gate_name=f"{stage.value}_codex_review",
        stage=stage,
        status=GateStatus.PASSED if verdict == "PASS" else GateStatus.FAILED,
        checks=[GateCheck(
            name="codex_review", description="Codex adversarial review (gpt-5.4 xhigh)",
            check_type="codex", passed=verdict == "PASS",
            feedback=result.output_text,
        )],
        reviewer=AgentRole.CRITIC,
        overall_feedback=result.output_text,
        iteration=state.iteration_count.get(stage.value, 1),
    )

    state = sm.load_project(project_id)
    state.gate_results.append(gate_result)
    evt = VersionEventType.GATE_PASSED if verdict == "PASS" else VersionEventType.GATE_FAILED
    state.record_event(
        evt,
        f"Codex verdict: {verdict}",
        agent=AgentRole.CRITIC,
        gate_verdict=verdict,
        duration_seconds=result.duration_seconds,
        detail=result.output_text,
    )
    sm.save_project(state)

    icon = "✓" if verdict == "PASS" else "✗"
    print(f"┌─ {icon} v{ver} Codex: {verdict} ({result.duration_seconds:.1f}s)")
    print(f"└─")
    print()
    return result, gate_result


# ---------------------------------------------------------------------------
# Full automated pipeline (no human confirmation)
# ---------------------------------------------------------------------------

def run_auto(sm, dispatcher, project_id, until_stage=None, max_revisions=3, instruction=""):
    """Fully automatic — no confirmation prompts."""
    config = load_config()
    human_gates = [Stage(s) for s in config.get("pipeline", {}).get("human_gates",
                   ["hypothesis_formation", "experimentation"])]

    while True:
        state = sm.load_project(project_id)
        stage = state.current_stage

        if until_stage and STAGE_ORDER.index(stage) > STAGE_ORDER.index(until_stage):
            print(f"\n=== Reached {until_stage.value}. Stopping. ===")
            break
        if stage == STAGE_ORDER[-1]:
            gates = [g for g in state.gate_results if g.stage == stage]
            if gates and gates[-1].status == GateStatus.PASSED:
                print("\n=== Pipeline complete! ===")
                break

        ver = state.current_version()
        print(f"\n{'='*60}")
        print(f"  v{ver}  STAGE: {stage.value}")
        print(f"{'='*60}\n")

        for rev in range(max_revisions + 1):
            result, _ = run_step(sm, dispatcher, project_id, instruction, auto_mode=True)

            # Auth error → stop entire auto pipeline, let user fix
            if result.is_auth_error:
                print(f"\n  ✗ Pipeline paused: authentication error.")
                print(f"  ✗ Fix with: /login  or  claude login")
                print(f"  ✗ Then resume: python scripts/multi_agent.py auto")
                return

            if not result.success and not result.output_files:
                continue

            codex_result, gate_result = run_review(sm, dispatcher, project_id, auto_mode=True)

            # Codex auth error → stop
            if codex_result.is_auth_error:
                print(f"\n  ✗ Pipeline paused: Codex authentication error.")
                print(f"  ✗ Fix with: codex login")
                print(f"  ✗ Then resume: python scripts/multi_agent.py auto")
                return

            if gate_result and gate_result.status == GateStatus.PASSED:
                break
            if rev < max_revisions:
                # Increment iteration count so version number advances (v0.1 → v0.2)
                # and max_iterations gate check works
                state = sm.load_project(project_id)
                state.increment_iteration()
                sm.save_project(state)
                print(f"  Revision {rev+1}/{max_revisions} → v{state.current_version()}...\n")
                instruction = ""

        state = sm.load_project(project_id)
        gates = [g for g in state.gate_results if g.stage == state.current_stage]
        latest = gates[-1] if gates else None

        if latest and latest.status != GateStatus.PASSED:
            print(f"\n  ✗ Gate not passed for {stage.value}.")
            print(f"  ✗ Fix issues, then resume: python scripts/multi_agent.py auto")
            break

        if stage in human_gates:
            # Persist HUMAN_REVIEW status so advance --approve is required
            state = sm.load_project(project_id)
            gates = [g for g in state.gate_results if g.stage == stage]
            if gates:
                gates[-1].status = GateStatus.HUMAN_REVIEW
            state.record_event(VersionEventType.GATE_REVIEW,
                f"Human gate: awaiting approval at {stage.value}")
            sm.save_project(state)
            print(f"\n  Human gate at {stage.value}. Run: ra advance --approve")
            break

        idx = STAGE_ORDER.index(stage)
        if idx < len(STAGE_ORDER) - 1:
            nxt = STAGE_ORDER[idx + 1]
            trigger = ALLOWED_TRANSITIONS.get((stage, nxt), "auto_advance")
            state.record_transition(nxt, trigger, gate_result=latest)
            sm.save_project(state)
            print(f"\n>>> v{state.current_version()} Advanced → {nxt.value}\n")
            instruction = ""
        else:
            print(f"\n=== Done! ===")
            break


# ---------------------------------------------------------------------------
# Advance-mode step with human confirmation
# ---------------------------------------------------------------------------

def run_advance_step(sm, dispatcher, project_id, instruction=""):
    """Single step with human confirmation before every version bump."""
    state = sm.load_project(project_id)
    stage = state.current_stage

    # Run agent
    result, msg = run_step(sm, dispatcher, project_id, instruction, auto_mode=False)

    if not result.success and not result.output_files:
        print("Agent produced no output.")
        return

    # Run review
    _, gate_result = run_review(sm, dispatcher, project_id, auto_mode=False)

    if not gate_result or gate_result.status != GateStatus.PASSED:
        print(f"\nGate not passed. Address feedback and run step again.")
        return

    # Confirm major version bump (stage advance)
    state = sm.load_project(project_id)
    idx = STAGE_ORDER.index(stage)
    if idx < len(STAGE_ORDER) - 1:
        nxt = STAGE_ORDER[idx + 1]
        approved, feedback = confirm_version_bump(
            state, "MAJOR",
            f"Advance: {stage.value} → {nxt.value}  (v{state.current_version()} → v{idx+1}.1)",
            sm, project_id,
        )
        if approved:
            state = sm.load_project(project_id)
            trigger = ALLOWED_TRANSITIONS.get((stage, nxt), "advance")
            state.record_transition(nxt, trigger, gate_result=gate_result)
            sm.save_project(state)
            print(f"\n>>> v{state.current_version()} Advanced → {nxt.value}")
        else:
            if feedback:
                print(f"\nFeedback recorded. Run step again with updated plan.")
                state = sm.load_project(project_id)
                state.record_event(VersionEventType.HUMAN_FEEDBACK, f"Guidance: {feedback[:100]}", detail=feedback)
                sm.save_project(state)
    else:
        print(f"\nFinal stage complete!")


# ---------------------------------------------------------------------------
# Timeline display
# ---------------------------------------------------------------------------

def show_timeline(sm, project_id):
    state = sm.load_project(project_id)
    if not state.timeline:
        print("No events yet.")
        return

    print(f"\n  Version Timeline: {state.name}")
    print(f"  {'─'*70}")

    current_ver = ""
    for ev in state.timeline:
        # Version header
        if ev.version != current_ver:
            current_ver = ev.version
            print(f"\n  v{current_ver}  ({'─'*50})")

        icon = {
            VersionEventType.AGENT_RUN: "▶",
            VersionEventType.GATE_REVIEW: "◆",
            VersionEventType.GATE_PASSED: "✓",
            VersionEventType.GATE_FAILED: "✗",
            VersionEventType.STAGE_ADVANCE: "⏩",
            VersionEventType.STAGE_ROLLBACK: "↩",
            VersionEventType.HUMAN_APPROVE: "👤✓",
            VersionEventType.HUMAN_REJECT: "👤✗",
            VersionEventType.HUMAN_FEEDBACK: "👤💬",
        }.get(ev.event_type, "·")

        agent_str = f"[{ev.agent.value}]" if ev.agent else ""
        cost_str = f" ${ev.cost_usd:.3f}" if ev.cost_usd > 0 else ""
        time_str = ev.timestamp.strftime("%H:%M:%S")
        verdict_str = f" → {ev.gate_verdict}" if ev.gate_verdict else ""

        print(f"    {icon} {time_str} {agent_str:<14s} {ev.summary}{verdict_str}{cost_str}")

        if ev.artifacts_produced:
            for a in ev.artifacts_produced:
                print(f"      📄 {Path(a).name}")

    print(f"\n  {'─'*70}")
    print(f"  Current: v{state.current_version()} | Cost: ${state.total_cost():.4f}")
    print()


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def show_status(sm, project_id):
    state = sm.load_project(project_id)
    stage = state.current_stage
    ci = STAGE_ORDER.index(stage)

    print(f"\n  Project: {state.name}  (v{state.current_version()})")
    print(f"  ID:      {project_id}")
    print(f"  Question: {state.research_question}\n")

    for i, s in enumerate(STAGE_ORDER):
        icon = "  ✓" if i < ci else ("  →" if i == ci else "  ○")
        role = STAGE_PRIMARY_AGENT[s].value
        arts = len(state.stage_artifacts(s))
        gates = [g for g in state.gate_results if g.stage == s]
        gs = f" [{gates[-1].status.value}]" if gates else ""
        print(f"{icon} v{i}.x {s.value:<23s} agent={role:<12s} artifacts={arts}{gs}")

    config = load_config()
    critic_cfg = config.get("agents", {}).get("critic", {})
    critic_label = f"{critic_cfg.get('backend', 'codex')}/{critic_cfg.get('model', 'gpt-5.4')}"
    print(f"\n  Version: v{state.current_version()} | Cost: ${state.total_cost():.4f}")
    print(f"  Events:  {len(state.timeline)} | Critic: {critic_label}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Research Pipeline")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")

    p_step = sub.add_parser("step", help="One step with human confirmation")
    p_step.add_argument("--instruction", "-i", default="")
    p_step.add_argument("--stage", "-s", default=None)

    sub.add_parser("review", help="Run Codex critic")

    p_auto = sub.add_parser("auto", help="Full auto (no confirmation)")
    p_auto.add_argument("--until", default=None)
    p_auto.add_argument("--max-revisions", "-n", type=int, default=3)
    p_auto.add_argument("--instruction", "-i", default="")

    sub.add_parser("timeline", help="Print version timeline")

    p_gui = sub.add_parser("gui", help="Launch web GUI")
    p_gui.add_argument("--port", "-p", type=int, default=8080)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    config = load_config()
    sm = StateManager(ROOT)
    project_id = get_active(sm)
    dispatcher = MultiAgentDispatcher(ROOT, ROOT / "agents", config)

    if args.command == "status":
        show_status(sm, project_id)
    elif args.command == "step":
        run_advance_step(sm, dispatcher, project_id, args.instruction)
    elif args.command == "review":
        run_review(sm, dispatcher, project_id)
    elif args.command == "auto":
        until = Stage(args.until) if args.until else None
        run_auto(sm, dispatcher, project_id, until, args.max_revisions, args.instruction)
    elif args.command == "timeline":
        show_timeline(sm, project_id)
    elif args.command == "gui":
        from research_agent.gui import run_gui
        run_gui(sm, project_id, config, port=args.port)


if __name__ == "__main__":
    main()
