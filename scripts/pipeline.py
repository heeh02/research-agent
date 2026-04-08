#!/usr/bin/env python3
"""Pipeline Manager — CLI for Claude Code to manage research pipeline state.

This is the bridge between Claude Code and the research agent system.
Claude Code calls this script to check state, advance stages, and track costs.

Commands:
    python scripts/pipeline.py status              Show current state
    python scripts/pipeline.py init <name>         Create project
    python scripts/pipeline.py run [--instruction]  Show what to do next
    python scripts/pipeline.py save <type> <file>  Register an artifact
    python scripts/pipeline.py advance             Advance to next stage
    python scripts/pipeline.py rollback <stage>    Roll back
    python scripts/pipeline.py cost                Show cost breakdown
    python scripts/pipeline.py validate <file>     Validate artifact against schema
    python scripts/pipeline.py context             Output full context for current stage
"""

from __future__ import annotations

import argparse
import json
import sys
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
    STAGE_REVIEWER,
    AgentRole,
    Artifact,
    ArtifactType,
    GateResult,
    GateStatus,
    ProjectState,
    Stage,
)
from research_agent.state import StateManager
from research_agent.artifacts import (
    assemble_context,
    create_artifact,
    load_schema,
    validate_artifact_content,
)


def get_sm() -> StateManager:
    return StateManager(ROOT)


def get_active() -> tuple[StateManager, str]:
    sm = get_sm()
    active_file = ROOT / ".active_project"
    if not active_file.exists():
        print("No active project. Run: python scripts/pipeline.py init <name>")
        sys.exit(1)
    return sm, active_file.read_text().strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    sm = get_sm()
    state = sm.create_project(args.name, args.description or "", args.question or "")
    (ROOT / ".active_project").write_text(state.project_id)
    print(f"Project created: {state.name}")
    print(f"ID: {state.project_id}")
    print(f"Stage: {state.current_stage.value}")
    print(f"\nNext: produce a problem_brief.yaml artifact")


def cmd_status(args):
    sm, pid = get_active()
    state = sm.load_project(pid)
    stage = state.current_stage
    iteration = state.current_iteration()

    print(f"╔══════════════════════════════════════════════════╗")
    print(f"║  Project: {state.name[:38]:<38s}  ║")
    print(f"║  ID:      {pid[:38]:<38s}  ║")
    print(f"║  Question: {(state.research_question or 'N/A')[:37]:<37s}  ║")
    print(f"╠══════════════════════════════════════════════════╣")

    current_idx = STAGE_ORDER.index(stage)
    for i, s in enumerate(STAGE_ORDER):
        if i < current_idx:
            icon = "✓"
        elif i == current_idx:
            icon = "→"
        else:
            icon = "○"
        # Gate info
        gates = [g for g in state.gate_results if g.stage == s]
        gate_str = ""
        if gates:
            latest = gates[-1]
            gate_str = f" [{latest.status.value}]"
        artifacts = state.stage_artifacts(s)
        art_str = f" ({len(artifacts)} artifacts)" if artifacts else ""
        print(f"║  {icon} {s.value:<25s}{gate_str:<12s}{art_str:<10s} ║")

    print(f"╠══════════════════════════════════════════════════╣")
    print(f"║  Current: {stage.value:<20s} iter: {iteration:<8d}  ║")
    print(f"║  Cost:    ${state.total_cost():<18.4f}                   ║")
    print(f"╚══════════════════════════════════════════════════╝")

    # Show what's needed next
    required = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
    missing = [a.value for a in required if state.latest_artifact(a) is None]
    if missing:
        print(f"\n⚠  Missing artifacts for current stage: {', '.join(missing)}")
    else:
        print(f"\n✓ All required artifacts present. Request Codex review: /codex:adversarial-review  or  python scripts/codex_review.py")

    # Latest gate feedback
    stage_gates = [g for g in state.gate_results if g.stage == stage]
    if stage_gates:
        latest = stage_gates[-1]
        if latest.status == GateStatus.FAILED:
            print(f"\n✗ Last gate FAILED:")
            print(latest.overall_feedback)


def cmd_run(args):
    """Show detailed next-step instructions for the current stage."""
    sm, pid = get_active()
    state = sm.load_project(pid)
    stage = state.current_stage
    required = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
    missing = [a for a in required if state.latest_artifact(a) is None]
    primary = STAGE_PRIMARY_AGENT.get(stage, AgentRole.RESEARCHER)

    print(f"Stage: {stage.value}")
    print(f"Primary role: {primary.value} (that's you, Claude Code)")
    print()

    if missing:
        print(f"TODO: Produce these artifacts:")
        for atype in missing:
            schema = load_schema(ROOT / "schemas", atype)
            req_fields = schema.get("required_fields", []) if schema else []
            template_path = ROOT / "templates" / f"{atype.value}.yaml"
            print(f"\n  [{atype.value}]")
            if req_fields:
                print(f"    Required fields: {', '.join(req_fields)}")
            if template_path.exists():
                print(f"    Template: {template_path}")
            print(f"    Save to: projects/{pid}/artifacts/{stage.value}/{atype.value}_v1.yaml")
            print(f"    Register: python scripts/pipeline.py save {atype.value} <filepath>")
    else:
        print("All artifacts ready. Next steps:")
        print(f"  1. Review: python scripts/gpt_review.py")
        print(f"  2. If PASS: python scripts/pipeline.py advance")
        print(f"  3. If REVISE: fix artifacts, re-register, re-review")

    # Show previous feedback if iteration > 1
    stage_gates = [g for g in state.gate_results if g.stage == stage]
    if stage_gates:
        latest = stage_gates[-1]
        print(f"\n{'='*50}")
        print(f"Previous review feedback (iteration {latest.iteration}):")
        print(latest.overall_feedback)


def cmd_save(args):
    """Register an artifact file with the pipeline."""
    sm, pid = get_active()
    state = sm.load_project(pid)
    stage = state.current_stage

    try:
        atype = ArtifactType(args.type)
    except ValueError:
        print(f"Invalid artifact type: {args.type}")
        print(f"Valid types: {', '.join(t.value for t in ArtifactType)}")
        sys.exit(1)

    src = Path(args.file)
    if not src.exists():
        print(f"File not found: {src}")
        sys.exit(1)

    content = src.read_text()

    # Validate against schema
    schema = load_schema(ROOT / "schemas", atype)
    if schema:
        errors = validate_artifact_content(content, schema)
        if errors:
            print(f"⚠  Schema validation warnings:")
            for e in errors:
                print(f"    - {e}")
            if not args.force:
                print("Use --force to register anyway.")
                sys.exit(1)

    # Determine version
    existing = [a for a in state.artifacts if a.artifact_type == atype]
    version = max((a.version for a in existing), default=0) + 1
    filename = f"{atype.value}_v{version}.yaml"

    # Copy to project artifacts dir
    sm.save_artifact_file(pid, stage, filename, content)

    # Register in state
    artifact = create_artifact(state, atype, stage, AgentRole.ENGINEER, filename)

    sm.save_project(state)
    print(f"✓ Registered: {atype.value} v{version} at {artifact.path}")


def cmd_advance(args):
    sm, pid = get_active()
    state = sm.load_project(pid)
    stage = state.current_stage
    stage_idx = STAGE_ORDER.index(stage)

    if stage_idx >= len(STAGE_ORDER) - 1:
        print("Already at final stage (analysis). Pipeline complete!")
        return

    # Check gate
    stage_gates = [g for g in state.gate_results if g.stage == stage]
    if not stage_gates and not args.force:
        print("No gate evaluation found. Run Codex review first:")
        print(f"  /codex:adversarial-review  or  python scripts/codex_review.py")
        sys.exit(1)

    if stage_gates:
        latest = stage_gates[-1]
        if latest.status == GateStatus.FAILED and not args.force:
            print(f"Gate FAILED. Fix issues first.")
            print(latest.overall_feedback)
            sys.exit(1)
        if latest.status == GateStatus.HUMAN_REVIEW and not args.approve:
            print("Gate requires human approval. Use --approve to confirm.")
            sys.exit(1)

    next_stage = STAGE_ORDER[stage_idx + 1]
    trigger = ALLOWED_TRANSITIONS.get((stage, next_stage), "manual_advance")
    state.record_transition(next_stage, trigger)
    sm.save_project(state)
    print(f"✓ Advanced: {stage.value} → {next_stage.value}")

    # Show next steps
    required = STAGE_REQUIRED_ARTIFACTS.get(next_stage, [])
    if required:
        print(f"\nNext stage requires: {', '.join(a.value for a in required)}")
        print(f"Run: python scripts/pipeline.py run")


def cmd_rollback(args):
    sm, pid = get_active()
    state = sm.load_project(pid)

    try:
        target = Stage(args.stage)
    except ValueError:
        print(f"Invalid stage: {args.stage}")
        print(f"Valid: {', '.join(s.value for s in Stage)}")
        sys.exit(1)

    key = (state.current_stage, target)
    if key not in ALLOWED_TRANSITIONS:
        print(f"Cannot roll back from {state.current_stage.value} to {target.value}")
        print("Allowed rollbacks from current stage:")
        for (f, t), trigger in ALLOWED_TRANSITIONS.items():
            if f == state.current_stage and STAGE_ORDER.index(t) < STAGE_ORDER.index(f):
                print(f"  → {t.value} ({trigger})")
        sys.exit(1)

    trigger = ALLOWED_TRANSITIONS[key]
    state.record_transition(target, trigger, notes=args.reason or "")
    sm.save_project(state)
    print(f"↩ Rolled back: {state.current_stage.value} (iteration {state.current_iteration()})")


def cmd_cost(args):
    sm, pid = get_active()
    state = sm.load_project(pid)

    if not state.cost_records:
        print("No costs yet.")
        return

    by_stage: dict[str, float] = {}
    by_agent: dict[str, float] = {}
    by_model: dict[str, float] = {}
    for r in state.cost_records:
        by_stage[r.stage.value] = by_stage.get(r.stage.value, 0) + r.cost_usd
        by_agent[r.agent.value] = by_agent.get(r.agent.value, 0) + r.cost_usd
        by_model[r.model] = by_model.get(r.model, 0) + r.cost_usd

    print(f"Total: ${state.total_cost():.4f}")
    print(f"\nBy Stage:")
    for s, c in sorted(by_stage.items()):
        print(f"  {s:<25s} ${c:.4f}")
    print(f"\nBy Agent:")
    for a, c in sorted(by_agent.items()):
        print(f"  {a:<15s} ${c:.4f}")
    print(f"\nBy Model:")
    for m, c in sorted(by_model.items()):
        print(f"  {m:<30s} ${c:.4f}")


def cmd_validate(args):
    """Validate an artifact file against its schema."""
    filepath = Path(args.file)
    if not filepath.exists():
        print(f"File not found: {filepath}")
        sys.exit(1)

    # Infer artifact type from filename
    name = filepath.stem.split("_v")[0]  # e.g., "problem_brief_v1" -> "problem_brief"
    try:
        atype = ArtifactType(name)
    except ValueError:
        print(f"Cannot infer artifact type from filename: {filepath.name}")
        print(f"Expected pattern: <type>_v<N>.yaml")
        sys.exit(1)

    schema = load_schema(ROOT / "schemas", atype)
    if not schema:
        print(f"No schema found for {atype.value}")
        sys.exit(0)

    content = filepath.read_text()
    errors = validate_artifact_content(content, schema)

    if errors:
        print(f"✗ Validation FAILED for {atype.value}:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print(f"✓ {atype.value} passes schema validation")


def cmd_context(args):
    """Output the full assembled context for Claude Code to consume."""
    sm, pid = get_active()
    state = sm.load_project(pid)
    stage = state.current_stage

    # Get all relevant artifacts
    from research_agent.models import STAGE_ORDER
    stage_idx = STAGE_ORDER.index(stage)
    relevant_types: list[ArtifactType] = []
    for s in STAGE_ORDER[:stage_idx + 1]:
        relevant_types.extend(STAGE_REQUIRED_ARTIFACTS.get(s, []))

    artifact_contents = sm.get_latest_artifacts(state, relevant_types)
    context = assemble_context(state, artifact_contents, stage)
    print(context)


# ---------------------------------------------------------------------------
# Repair — fix version/path mismatches and remove invalid artifacts
# ---------------------------------------------------------------------------

def cmd_repair(args):
    """Repair state.json: fix artifact version/path mismatches, remove invalid entries."""
    sm = StateManager(ROOT / "projects")
    project_id = get_active(sm)
    state = sm.load_project(project_id)
    project_dir = ROOT / "projects" / project_id

    fixed = 0
    removed = 0
    valid_artifacts = []

    for art in state.artifacts:
        art_path = project_dir / art.path
        # Check 1: file exists
        if not art_path.exists():
            print(f"  REMOVE: {art.name} — file not found: {art.path}")
            removed += 1
            continue

        # Check 2: valid YAML
        try:
            raw = art_path.read_text(encoding="utf-8")
            yaml.safe_load(raw)
        except Exception as e:
            print(f"  REMOVE: {art.name} — invalid YAML: {e}")
            removed += 1
            continue

        # Check 3: version/path consistency
        expected_filename = f"{art.artifact_type.value}_v{art.version}.yaml"
        expected_path = f"artifacts/{art.stage.value}/{expected_filename}"
        if art.path != expected_path:
            # Try to rename file to match version
            expected_full = project_dir / expected_path
            if not expected_full.exists():
                art_path.rename(expected_full)
                print(f"  FIX: {art.name} — renamed {art.path} → {expected_path}")
                art.path = expected_path
                fixed += 1
            else:
                print(f"  REMOVE: {art.name} — path mismatch and target exists: {art.path} ≠ {expected_path}")
                removed += 1
                continue

        valid_artifacts.append(art)

    # Deduplicate: keep only latest version of each type
    seen: dict[str, int] = {}
    deduped = []
    for art in reversed(valid_artifacts):
        key = art.artifact_type.value
        if key not in seen or art.version > seen[key]:
            seen[key] = art.version
        deduped.append(art)
    deduped.reverse()

    state.artifacts = deduped
    sm.save_project(state)

    print(f"\nRepair complete: {fixed} fixed, {removed} removed, {len(deduped)} artifacts retained.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Research Pipeline Manager")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Create new project")
    p_init.add_argument("name")
    p_init.add_argument("--question", "-q", help="Research question")
    p_init.add_argument("--description", "-d", help="Description")

    sub.add_parser("status", help="Show project status")
    sub.add_parser("run", help="Show next steps")

    p_save = sub.add_parser("save", help="Register artifact")
    p_save.add_argument("type", help="Artifact type")
    p_save.add_argument("file", help="Artifact file path")
    p_save.add_argument("--force", action="store_true")

    p_adv = sub.add_parser("advance", help="Advance to next stage")
    p_adv.add_argument("--force", action="store_true")
    p_adv.add_argument("--approve", action="store_true")

    p_rb = sub.add_parser("rollback", help="Roll back to earlier stage")
    p_rb.add_argument("stage")
    p_rb.add_argument("--reason", "-r", default="")

    sub.add_parser("cost", help="Show cost breakdown")

    p_val = sub.add_parser("validate", help="Validate artifact file")
    p_val.add_argument("file")

    sub.add_parser("context", help="Output assembled context")
    sub.add_parser("repair", help="Fix artifact version/path mismatches and remove invalid entries")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    {
        "init": cmd_init,
        "status": cmd_status,
        "run": cmd_run,
        "save": cmd_save,
        "advance": cmd_advance,
        "rollback": cmd_rollback,
        "cost": cmd_cost,
        "validate": cmd_validate,
        "context": cmd_context,
        "repair": cmd_repair,
    }[args.command](args)


if __name__ == "__main__":
    main()
