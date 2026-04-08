"""Role-scoped workspace monitoring for agent isolation.

This module provides file-system snapshot + diff based violation detection.
It does NOT provide hard isolation (no containers, no OS permissions).
It detects and records when an agent writes outside its allowed paths.

Design: before dispatch, snapshot the project directory.  After dispatch,
diff the snapshot to find new/modified files.  Any file not in the
agent's "allowed write set" is flagged as a violation.

Allowed write sets per role:
  - Researcher: artifacts/<current_stage>/*.yaml
  - Engineer:   artifacts/<current_stage>/*.yaml, experiments/**
  - Critic:     NOTHING (critic must not write files; output is stdout only)
  - Orchestrator: artifacts/<current_stage>/*.yaml, logs/**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import AgentRole, Stage


@dataclass
class FileSnapshot:
    """Snapshot of files under a directory: {relative_path: mtime}."""
    files: dict[str, float] = field(default_factory=dict)


@dataclass
class Violation:
    """A single isolation violation."""
    path: str           # Relative path of the unauthorized file
    kind: str           # "created" or "modified"
    role: AgentRole
    stage: Stage


@dataclass
class ViolationReport:
    """Result of checking an agent's workspace for violations."""
    role: AgentRole
    stage: Stage
    violations: list[Violation] = field(default_factory=list)
    allowed_patterns: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return len(self.violations) == 0

    def summary(self) -> str:
        if self.clean:
            return f"{self.role.value}: no violations"
        lines = [f"{self.role.value}: {len(self.violations)} violation(s)"]
        for v in self.violations:
            lines.append(f"  [{v.kind}] {v.path}")
        return "\n".join(lines)


# Directories to always exclude from workspace scanning
_SNAPSHOT_EXCLUDE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

# Filenames / suffixes to always exclude
_SNAPSHOT_EXCLUDE_NAMES = frozenset({"state.json", ".DS_Store"})
_SNAPSHOT_EXCLUDE_SUFFIXES = (".tmp", ".lock", ".pyc", ".pyo")


def snapshot_directory(base_dir: Path, project_id: str) -> FileSnapshot:
    """Snapshot all files under base_dir using a denylist approach.

    Scans everything and excludes:
    - Known non-content directories (.git, __pycache__, node_modules, ...)
    - Other projects (projects/<other_id>/...)
    - State/temp/bytecode files

    This ensures writes to ANY repo path (including top-level directories
    and root control files) are detectable as violations.
    """
    snap = FileSnapshot()
    if not base_dir.exists():
        return snap

    for f in base_dir.rglob("*"):
        if not f.is_file():
            continue

        rel_parts = f.relative_to(base_dir).parts

        # Skip excluded directories
        if any(p in _SNAPSHOT_EXCLUDE_DIRS or p.endswith(".egg-info") for p in rel_parts):
            continue

        # Skip other projects (only monitor current project + non-project files)
        if len(rel_parts) >= 2 and rel_parts[0] == "projects" and rel_parts[1] != project_id:
            continue

        # Skip state/temp/bytecode files
        if f.name in _SNAPSHOT_EXCLUDE_NAMES or f.suffix in _SNAPSHOT_EXCLUDE_SUFFIXES:
            continue

        rel = str(f.relative_to(base_dir))
        try:
            snap.files[rel] = f.stat().st_mtime
        except OSError:
            pass

    return snap


def check_violations(
    before: FileSnapshot,
    after: FileSnapshot,
    role: AgentRole,
    stage: Stage,
    expected_outputs: list[str],
    project_id: str,
) -> ViolationReport:
    """Compare before/after snapshots and flag unauthorized writes.

    A write is authorized if:
    1. The file path is in expected_outputs (the task card's required outputs), OR
    2. The file matches the role's allowed write patterns for the stage.

    Everything else is a violation.
    """
    allowed = _allowed_write_patterns(role, stage, project_id)
    report = ViolationReport(role=role, stage=stage, allowed_patterns=allowed)

    # Normalize expected_outputs to be relative to base_dir
    normalized_expected: set[str] = set()
    prefix = f"projects/{project_id}/"
    for p in expected_outputs:
        if p.startswith(prefix):
            normalized_expected.add(p)
        else:
            normalized_expected.add(prefix + p)

    # Find new or modified files
    for path, mtime in after.files.items():
        old_mtime = before.files.get(path)
        if old_mtime is not None and abs(mtime - old_mtime) < 0.01:
            continue  # Unchanged

        kind = "modified" if old_mtime is not None else "created"

        # Check against expected outputs
        if path in normalized_expected:
            continue

        # Check against role patterns
        if _matches_any_pattern(path, allowed):
            continue

        report.violations.append(Violation(
            path=path, kind=kind, role=role, stage=stage,
        ))

    return report


def _allowed_write_patterns(role: AgentRole, stage: Stage, project_id: str = "") -> list[str]:
    """Return path prefixes (relative to base_dir) that a role is allowed to write."""
    proj = f"projects/{project_id}/" if project_id else ""
    stage_art = f"{proj}artifacts/{stage.value}/"
    stage_log = f"{proj}logs/"

    if role == AgentRole.RESEARCHER:
        return [stage_art]
    elif role == AgentRole.ENGINEER:
        return [stage_art, f"{proj}experiments/"]
    elif role == AgentRole.CRITIC:
        # Critic should write NOTHING — all output is stdout
        # We still allow review_*.yaml in the stage dir (dispatcher writes these)
        return [f"{proj}artifacts/{stage.value}/review_"]
    elif role == AgentRole.ORCHESTRATOR:
        return [stage_art, stage_log]
    return [stage_art]


def _matches_any_pattern(path: str, patterns: list[str]) -> bool:
    """Check if path starts with any of the allowed prefixes."""
    return any(path.startswith(p) for p in patterns)
