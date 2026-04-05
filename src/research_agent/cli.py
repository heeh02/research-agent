"""CLI interface — project state management only.

Agent dispatch goes through `scripts/multi_agent.py`.
This CLI handles: init, status, save, advance, rollback, artifacts, cost, history.

Usage:
    ra init <name> -q "question"     Create a new research project
    ra status                        Show current project status
    ra save <type> <file>           Register an artifact
    ra advance [--approve] [--force] Advance to next stage
    ra rollback <stage>              Roll back to an earlier stage
    ra artifacts                     List all artifacts
    ra cost                          Show cost breakdown
    ra history                       Show project history
    ra projects                      List all projects
    ra use <project_id>              Switch to a project
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from .models import (
    ALLOWED_TRANSITIONS,
    STAGE_ORDER,
    STAGE_PRIMARY_AGENT,
    STAGE_REQUIRED_ARTIFACTS,
    AgentRole,
    ArtifactType,
    GateStatus,
    Stage,
)
from .state import StateManager
from .artifacts import create_artifact, load_schema, validate_artifact_content

console = Console()


def _find_base_dir() -> Path:
    cwd = Path.cwd()
    for d in [cwd] + list(cwd.parents):
        if (d / "config" / "settings.yaml").exists():
            return d
    return cwd


def _get_sm(base_dir: Optional[Path] = None) -> StateManager:
    return StateManager(base_dir or _find_base_dir())


def _get_active(base_dir: Path) -> Optional[str]:
    f = base_dir / ".active_project"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def _set_active(base_dir: Path, project_id: str) -> None:
    (base_dir / ".active_project").write_text(project_id, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option("--base-dir", type=click.Path(exists=True), default=None)
@click.pass_context
def main(ctx, base_dir):
    """Research Agent — project state management."""
    ctx.ensure_object(dict)
    ctx.obj["base_dir"] = Path(base_dir) if base_dir else _find_base_dir()


@main.command()
@click.argument("name")
@click.option("--question", "-q", prompt="Research question")
@click.option("--description", "-d", default="")
@click.pass_context
def init(ctx, name, question, description):
    """Create a new research project."""
    sm = _get_sm(ctx.obj["base_dir"])
    state = sm.create_project(name, description, question)
    _set_active(ctx.obj["base_dir"], state.project_id)
    console.print(f"[green]Created:[/] {state.name} ({state.project_id})")
    console.print(f"Next: [bold]python scripts/multi_agent.py auto[/]")


@main.command()
@click.pass_context
def status(ctx):
    """Show current project status."""
    sm = _get_sm(ctx.obj["base_dir"])
    pid = _get_active(ctx.obj["base_dir"])
    if not pid:
        console.print("[red]No active project. Run `ra init`.[/]")
        return

    state = sm.load_project(pid)
    stage = state.current_stage
    ci = STAGE_ORDER.index(stage)

    tree = Tree(f"[bold]{state.name}[/] v{state.current_version()}")
    for i, s in enumerate(STAGE_ORDER):
        icon = "[green]✓[/]" if i < ci else ("[yellow]→[/]" if i == ci else "[dim]○[/]")
        gates = [g for g in state.gate_results if g.stage == s]
        gs = f" [{gates[-1].status.value}]" if gates else ""
        arts = len(state.stage_artifacts(s))
        tree.add(f"{icon} {s.value} ({arts} artifacts){gs}")
    console.print(tree)

    table = Table(show_header=False, box=None)
    table.add_column("K", style="dim")
    table.add_column("V")
    table.add_row("Stage", f"{stage.value} (iter {state.current_iteration()})")
    table.add_row("Cost", f"${state.total_cost():.4f}")
    table.add_row("Events", str(len(state.timeline)))
    console.print(table)

    required = STAGE_REQUIRED_ARTIFACTS.get(stage, [])
    missing = [a.value for a in required if state.latest_artifact(a) is None]
    if missing:
        console.print(f"\n[yellow]Missing:[/] {', '.join(missing)}")
    else:
        console.print(f"\n[green]All artifacts present.[/] Run review or advance.")


@main.command()
@click.argument("type_name")
@click.argument("file_path")
@click.option("--force", is_flag=True)
@click.pass_context
def save(ctx, type_name, file_path, force):
    """Register an artifact file with the pipeline."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    pid = _get_active(bd)
    if not pid:
        console.print("[red]No active project.[/]")
        return

    state = sm.load_project(pid)
    try:
        atype = ArtifactType(type_name)
    except ValueError:
        console.print(f"[red]Invalid type: {type_name}[/]")
        return

    src = Path(file_path)
    if not src.exists():
        console.print(f"[red]File not found: {src}[/]")
        return

    content = src.read_text()
    schema = load_schema(bd / "schemas", atype)
    if schema:
        errors = validate_artifact_content(content, schema)
        if errors and not force:
            for e in errors:
                console.print(f"  [yellow]- {e}[/]")
            console.print("Use --force to register anyway.")
            return

    existing = [a for a in state.artifacts if a.artifact_type == atype]
    version = max((a.version for a in existing), default=0) + 1
    filename = f"{atype.value}_v{version}.yaml"
    sm.save_artifact_file(pid, state.current_stage, filename, content)
    create_artifact(state, atype, state.current_stage, AgentRole.RESEARCHER, filename)
    sm.save_project(state)
    console.print(f"[green]Saved:[/] {atype.value} v{version}")


@main.command()
@click.option("--approve", is_flag=True)
@click.option("--force", is_flag=True)
@click.pass_context
def advance(ctx, approve, force):
    """Advance to the next stage."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    pid = _get_active(bd)
    if not pid:
        console.print("[red]No active project.[/]")
        return

    state = sm.load_project(pid)
    stage = state.current_stage
    idx = STAGE_ORDER.index(stage)

    if idx >= len(STAGE_ORDER) - 1:
        console.print("Already at final stage.")
        return

    gates = [g for g in state.gate_results if g.stage == stage]
    if not gates and not force:
        console.print("[red]No gate result. Run review first.[/]")
        return

    if gates:
        latest = gates[-1]
        if latest.status == GateStatus.FAILED and not force:
            console.print(f"[red]Gate FAILED.[/] {latest.overall_feedback[:200]}")
            return
        if latest.status == GateStatus.HUMAN_REVIEW and not approve:
            console.print("[yellow]Human approval required. Use --approve.[/]")
            return
        if latest.status == GateStatus.HUMAN_REVIEW and approve:
            latest.status = GateStatus.PASSED
            sm.save_project(state)

    nxt = STAGE_ORDER[idx + 1]
    trigger = ALLOWED_TRANSITIONS.get((stage, nxt), "manual_advance")
    state.record_transition(nxt, trigger)
    sm.save_project(state)
    console.print(f"[green]Advanced → {nxt.value}[/] (v{state.current_version()})")


@main.command()
@click.argument("target_stage")
@click.option("--reason", "-r", default="")
@click.pass_context
def rollback(ctx, target_stage, reason):
    """Roll back to an earlier stage."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    pid = _get_active(bd)
    if not pid:
        console.print("[red]No active project.[/]")
        return

    state = sm.load_project(pid)
    try:
        target = Stage(target_stage)
    except ValueError:
        console.print(f"[red]Invalid stage: {target_stage}[/]")
        return

    key = (state.current_stage, target)
    if key not in ALLOWED_TRANSITIONS:
        console.print(f"[red]Cannot rollback {state.current_stage.value} → {target.value}[/]")
        return

    state.record_transition(target, ALLOWED_TRANSITIONS[key], notes=reason)
    sm.save_project(state)
    console.print(f"[yellow]Rolled back → {target.value}[/] (v{state.current_version()})")


@main.command()
@click.pass_context
def artifacts(ctx):
    """List all artifacts."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    pid = _get_active(bd)
    if not pid:
        return

    state = sm.load_project(pid)
    if not state.artifacts:
        console.print("[dim]No artifacts.[/]")
        return

    table = Table(title="Artifacts")
    table.add_column("Type")
    table.add_column("V")
    table.add_column("Stage")
    table.add_column("By")
    table.add_column("Path")
    for a in state.artifacts:
        table.add_row(a.artifact_type.value, f"v{a.version}", a.stage.value,
                       a.created_by.value, a.path)
    console.print(table)


@main.command()
@click.pass_context
def cost(ctx):
    """Show cost breakdown."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    pid = _get_active(bd)
    if not pid:
        return

    state = sm.load_project(pid)
    if not state.cost_records:
        console.print("[dim]No costs.[/]")
        return

    by_stage: dict[str, float] = {}
    for r in state.cost_records:
        by_stage[r.stage.value] = by_stage.get(r.stage.value, 0) + r.cost_usd
    table = Table(title="Cost by Stage")
    table.add_column("Stage")
    table.add_column("USD", justify="right")
    for s, c in sorted(by_stage.items()):
        table.add_row(s, f"${c:.4f}")
    table.add_row("[bold]Total[/]", f"[bold]${state.total_cost():.4f}[/]")
    console.print(table)


@main.command()
@click.pass_context
def history(ctx):
    """Show stage transitions."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    pid = _get_active(bd)
    if not pid:
        return

    state = sm.load_project(pid)
    if not state.transitions:
        console.print("[dim]No transitions.[/]")
        return

    table = Table(title="Transitions")
    table.add_column("Time")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Trigger")
    for t in state.transitions:
        table.add_row(
            t.timestamp.strftime("%m-%d %H:%M"),
            t.from_stage.value if t.from_stage else "—",
            t.to_stage.value, t.trigger,
        )
    console.print(table)


@main.command()
@click.pass_context
def projects(ctx):
    """List all projects."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    active = _get_active(bd)
    all_p = sm.list_projects()
    if not all_p:
        console.print("[dim]No projects.[/]")
        return

    table = Table(title="Projects")
    table.add_column("")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Stage")
    for p in all_p:
        table.add_row("→" if p.project_id == active else "",
                       p.project_id, p.name, p.current_stage.value)
    console.print(table)


@main.command()
@click.argument("project_id")
@click.pass_context
def use(ctx, project_id):
    """Switch to a project."""
    bd = ctx.obj["base_dir"]
    sm = _get_sm(bd)
    try:
        state = sm.load_project(project_id)
    except FileNotFoundError:
        console.print(f"[red]Not found: {project_id}[/]")
        return
    _set_active(bd, project_id)
    console.print(f"[green]Switched to: {state.name}[/]")


if __name__ == "__main__":
    main()
