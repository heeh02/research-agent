"""CLI interface for the research agent system.

Usage:
    ra init <name>                    Create a new research project
    ra status                         Show current project status
    ra run [--instruction TEXT]       Run the primary agent for current stage
    ra gate                           Evaluate the gate for current stage
    ra advance [--approve] [--force]  Advance to next stage
    ra rollback <stage>               Roll back to an earlier stage
    ra loop [--instruction TEXT]      Run agent→gate→revise loop automatically
    ra artifacts                      List all artifacts
    ra cost                           Show cost breakdown
    ra history                        Show project history
    ra projects                       List all projects
    ra use <project_id>               Switch to a different project
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from .models import STAGE_ORDER, Stage
from .orchestrator import Orchestrator

console = Console()

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _find_base_dir() -> Path:
    """Find the project base directory (looks for config/settings.yaml)."""
    # Check current directory first, then walk up
    cwd = Path.cwd()
    for d in [cwd] + list(cwd.parents):
        if (d / "config" / "settings.yaml").exists():
            return d
    return cwd


def _load_config(base_dir: Path) -> dict:
    config_file = base_dir / "config" / "settings.yaml"
    if config_file.exists():
        return yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    return {}


def _get_orchestrator(base_dir: Optional[Path] = None) -> Orchestrator:
    bd = base_dir or _find_base_dir()
    config = _load_config(bd)
    return Orchestrator(config, bd)


def _get_active_project_id(base_dir: Path) -> Optional[str]:
    active_file = base_dir / ".active_project"
    if active_file.exists():
        return active_file.read_text(encoding="utf-8").strip()
    return None


def _set_active_project_id(base_dir: Path, project_id: str) -> None:
    (base_dir / ".active_project").write_text(project_id, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@click.group()
@click.option("--base-dir", type=click.Path(exists=True), default=None, help="Project base directory")
@click.pass_context
def main(ctx, base_dir):
    """Research Agent — Multi-agent automated research pipeline."""
    ctx.ensure_object(dict)
    ctx.obj["base_dir"] = Path(base_dir) if base_dir else _find_base_dir()


@main.command()
@click.argument("name")
@click.option("--question", "-q", prompt="Research question", help="The main research question")
@click.option("--description", "-d", default="", help="Project description")
@click.pass_context
def init(ctx, name, question, description):
    """Create a new research project."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    state = orch.create_project(name, description, question)
    _set_active_project_id(ctx.obj["base_dir"], state.project_id)

    console.print(Panel(
        f"[bold green]Project created:[/] {state.name}\n"
        f"[dim]ID:[/] {state.project_id}\n"
        f"[dim]Question:[/] {state.research_question}\n"
        f"[dim]Stage:[/] {state.current_stage.value}\n\n"
        "Next: run [bold]ra run[/] to start the first stage.",
        title="New Research Project",
    ))


@main.command()
@click.pass_context
def status(ctx):
    """Show current project status."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project. Run `ra init` first.[/]")
        return

    state = orch.load_project(project_id)
    summary = orch.get_status_summary(state)

    # Build stage progress tree
    tree = Tree(f"[bold]{summary['project']}[/]")
    current_idx = STAGE_ORDER.index(state.current_stage)
    for i, stage in enumerate(STAGE_ORDER):
        if i < current_idx:
            icon = "[green]✓[/]"
        elif i == current_idx:
            icon = "[yellow]→[/]"
        else:
            icon = "[dim]○[/]"
        # Check if there's a gate result for this stage
        gate_info = ""
        stage_gates = [g for g in state.gate_results if g.stage == stage]
        if stage_gates:
            latest = stage_gates[-1]
            gate_info = f" [{latest.status.value}]"
        tree.add(f"{icon} {stage.value}{gate_info}")

    console.print(tree)
    console.print()

    # Summary table
    table = Table(show_header=False, box=None)
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("Stage", summary["stage"])
    table.add_row("Iteration", str(summary["iteration"]))
    table.add_row("Artifacts", str(summary["artifacts"]))
    table.add_row("Total Cost", summary["total_cost"])
    table.add_row("Latest Gate", summary["latest_gate"])
    console.print(table)


@main.command()
@click.option("--instruction", "-i", default="", help="Task instruction for the agent")
@click.option("--agent", "-a", type=click.Choice(["researcher", "critic", "engineer"]),
              default=None, help="Override which agent to use")
@click.pass_context
def run(ctx, instruction, agent):
    """Run the primary agent for the current stage."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project. Run `ra init` first.[/]")
        return

    state = orch.load_project(project_id)
    from .models import AgentRole
    agent_role = AgentRole(agent) if agent else None

    console.print(f"[dim]Running agent for stage: {state.current_stage.value}...[/]")

    output, state = orch.run_stage(state, instruction, agent_override=agent_role)

    console.print(Panel(output[:3000], title="Agent Output", border_style="blue"))
    if len(output) > 3000:
        console.print(f"[dim](output truncated, full output in artifacts)[/]")

    # Show cost
    if state.cost_records:
        latest_cost = state.cost_records[-1]
        console.print(f"[dim]Cost: ${latest_cost.cost_usd:.4f} ({latest_cost.model})[/]")


@main.command()
@click.pass_context
def gate(ctx):
    """Evaluate the gate for the current stage."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project. Run `ra init` first.[/]")
        return

    state = orch.load_project(project_id)
    console.print(f"[dim]Evaluating gate for stage: {state.current_stage.value}...[/]")

    result = orch.run_gate(state)

    # Display results
    status_color = {
        "passed": "green",
        "failed": "red",
        "human_review": "yellow",
        "pending": "dim",
    }.get(result.status.value, "white")

    console.print(Panel(
        f"[bold {status_color}]{result.status.value.upper()}[/]\n\n"
        f"{result.overall_feedback}",
        title=f"Gate: {result.gate_name}",
        border_style=status_color,
    ))

    # Show individual checks
    if result.checks:
        table = Table(title="Checks")
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Score")
        table.add_column("Feedback")
        for check in result.checks:
            status_icon = "[green]✓[/]" if check.passed else "[red]✗[/]"
            score_str = f"{check.score:.2f}" if check.score is not None else "—"
            table.add_row(check.name, status_icon, score_str, check.feedback[:80])
        console.print(table)


@main.command()
@click.option("--approve", is_flag=True, help="Approve human review gate")
@click.option("--force", is_flag=True, help="Force advance without gate check")
@click.pass_context
def advance(ctx, approve, force):
    """Advance to the next stage."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project. Run `ra init` first.[/]")
        return

    state = orch.load_project(project_id)

    if approve:
        # Mark the latest human_review gate as passed
        stage_gates = [g for g in state.gate_results if g.stage == state.current_stage]
        if stage_gates and stage_gates[-1].status.value == "human_review":
            stage_gates[-1].status = __import__(
                "research_agent.models", fromlist=["GateStatus"]
            ).GateStatus.PASSED
            orch.state_mgr.save_project(state)

    success, message = orch.advance(state, force=force)
    if success:
        console.print(f"[green]{message}[/]")
    else:
        console.print(f"[red]{message}[/]")


@main.command()
@click.argument("target_stage")
@click.option("--reason", "-r", default="", help="Reason for rollback")
@click.pass_context
def rollback(ctx, target_stage, reason):
    """Roll back to an earlier stage."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project. Run `ra init` first.[/]")
        return

    state = orch.load_project(project_id)
    try:
        target = Stage(target_stage)
    except ValueError:
        console.print(f"[red]Invalid stage: {target_stage}[/]")
        console.print(f"Valid stages: {', '.join(s.value for s in Stage)}")
        return

    success, message = orch.rollback(state, target, reason)
    if success:
        console.print(f"[yellow]{message}[/]")
    else:
        console.print(f"[red]{message}[/]")


@main.command()
@click.option("--instruction", "-i", default="", help="Task instruction")
@click.option("--max-revisions", "-n", default=3, help="Max revision cycles")
@click.pass_context
def loop(ctx, instruction, max_revisions):
    """Run agent→gate→revise loop automatically until gate passes."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project. Run `ra init` first.[/]")
        return

    state = orch.load_project(project_id)
    console.print(
        f"[dim]Running automated loop for stage: {state.current_stage.value} "
        f"(max {max_revisions} revisions)...[/]"
    )

    output, gate_result, state = orch.run_until_gate(state, instruction, max_revisions)

    status_color = "green" if gate_result.status.value in ("passed", "human_review") else "red"
    console.print(Panel(
        f"[bold {status_color}]{gate_result.status.value.upper()}[/] "
        f"after {gate_result.iteration} iteration(s)\n\n"
        f"{gate_result.overall_feedback}",
        title="Loop Result",
        border_style=status_color,
    ))


@main.command()
@click.pass_context
def artifacts(ctx):
    """List all artifacts in the current project."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project.[/]")
        return

    state = orch.load_project(project_id)
    if not state.artifacts:
        console.print("[dim]No artifacts yet.[/]")
        return

    table = Table(title="Artifacts")
    table.add_column("Type", style="bold")
    table.add_column("Version")
    table.add_column("Stage")
    table.add_column("Created By")
    table.add_column("Path")
    for a in state.artifacts:
        table.add_row(a.artifact_type.value, f"v{a.version}", a.stage.value,
                       a.created_by.value, a.path)
    console.print(table)


@main.command()
@click.pass_context
def cost(ctx):
    """Show cost breakdown by stage and agent."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project.[/]")
        return

    state = orch.load_project(project_id)
    if not state.cost_records:
        console.print("[dim]No costs recorded yet.[/]")
        return

    # By stage
    stage_costs: dict[str, float] = {}
    agent_costs: dict[str, float] = {}
    for r in state.cost_records:
        stage_costs[r.stage.value] = stage_costs.get(r.stage.value, 0) + r.cost_usd
        agent_costs[r.agent.value] = agent_costs.get(r.agent.value, 0) + r.cost_usd

    table = Table(title="Cost by Stage")
    table.add_column("Stage")
    table.add_column("Cost (USD)", justify="right")
    for stage, cost_val in sorted(stage_costs.items()):
        table.add_row(stage, f"${cost_val:.4f}")
    table.add_row("[bold]Total[/]", f"[bold]${state.total_cost():.4f}[/]")
    console.print(table)

    table2 = Table(title="Cost by Agent")
    table2.add_column("Agent")
    table2.add_column("Cost (USD)", justify="right")
    for agent_name, cost_val in sorted(agent_costs.items()):
        table2.add_row(agent_name, f"${cost_val:.4f}")
    console.print(table2)


@main.command()
@click.pass_context
def history(ctx):
    """Show project transition history."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    project_id = _get_active_project_id(ctx.obj["base_dir"])
    if not project_id:
        console.print("[red]No active project.[/]")
        return

    state = orch.load_project(project_id)
    if not state.transitions:
        console.print("[dim]No transitions yet.[/]")
        return

    table = Table(title="Stage Transitions")
    table.add_column("Time")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Trigger")
    table.add_column("Notes")
    for t in state.transitions:
        table.add_row(
            t.timestamp.strftime("%Y-%m-%d %H:%M"),
            t.from_stage.value if t.from_stage else "—",
            t.to_stage.value,
            t.trigger,
            t.notes[:40] if t.notes else "",
        )
    console.print(table)


@main.command()
@click.pass_context
def projects(ctx):
    """List all research projects."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    all_projects = orch.list_projects()
    active_id = _get_active_project_id(ctx.obj["base_dir"])

    if not all_projects:
        console.print("[dim]No projects. Run `ra init` to create one.[/]")
        return

    table = Table(title="Research Projects")
    table.add_column("Active")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Stage")
    table.add_column("Cost")
    for p in all_projects:
        active = "→" if p.project_id == active_id else ""
        table.add_row(active, p.project_id, p.name, p.current_stage.value,
                       f"${p.total_cost():.4f}")
    console.print(table)


@main.command()
@click.argument("project_id")
@click.pass_context
def use(ctx, project_id):
    """Switch to a different project."""
    orch = _get_orchestrator(ctx.obj["base_dir"])
    try:
        state = orch.load_project(project_id)
    except FileNotFoundError:
        console.print(f"[red]Project not found: {project_id}[/]")
        return
    _set_active_project_id(ctx.obj["base_dir"], project_id)
    console.print(f"[green]Switched to: {state.name} ({project_id})[/]")


if __name__ == "__main__":
    main()
