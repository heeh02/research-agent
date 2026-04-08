"""Shared pre-review structural checks and backend capability verification.

These checks are AUTHORITATIVE — if any blocking issue is found, the review
verdict will be overridden to REVISE regardless of what the critic says.
This ensures quality even when the critic backend is too lenient.

Used by both multi_agent.py (CLI path) and gui.py (web path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .models import (
    ArtifactType,
    AgentRole,
    CLIBackend,
    ProjectState,
    Stage,
)
from .state import StateManager


def _latest_artifacts_by_type(state: ProjectState, stage: Stage) -> list:
    """Return only the latest version of each artifact type for a stage.

    Prevents stale drafts from poisoning checks when a newer verified
    artifact exists.
    """
    seen: dict[str, "Artifact"] = {}  # type: ignore[name-defined]
    for art in state.stage_artifacts(stage):
        key = art.artifact_type.value
        if key not in seen or art.version > seen[key].version:
            seen[key] = art
    return list(seen.values())


def pre_review_checks(
    state: ProjectState,
    stage: Stage,
    state_manager: StateManager,
    project_id: str,
    base_dir: Path,
) -> list[str]:
    """Run automated structural checks before critic review.

    Returns list of blocking issue strings. Empty = all checks passed.
    """
    issues: list[str] = []

    if stage == Stage.LITERATURE_REVIEW:
        issues.extend(_check_literature(state, state_manager, project_id))

    if stage == Stage.IMPLEMENTATION:
        issues.extend(_check_implementation(state, state_manager, project_id, base_dir))

    if stage == Stage.EXPERIMENTATION:
        issues.extend(_check_experimentation(state, state_manager, project_id, base_dir))

    if stage == Stage.ANALYSIS:
        issues.extend(_check_analysis(state, state_manager, project_id, base_dir))

    return issues


def _check_literature(
    state: ProjectState,
    sm: StateManager,
    pid: str,
) -> list[str]:
    """Check literature_review artifacts for quality."""
    issues: list[str] = []
    for art in _latest_artifacts_by_type(state, Stage.LITERATURE_REVIEW):
        if art.artifact_type != ArtifactType.LITERATURE_MAP:
            continue
        try:
            content = yaml.safe_load(sm.read_artifact_file(pid, art))
            papers = (content or {}).get("papers", [])

            # Check URL presence
            missing_url = [p.get("title", "?") for p in papers if "url" not in p]
            if missing_url:
                issues.append(
                    f"{len(missing_url)}/{len(papers)} papers have no URL: "
                    + ", ".join(missing_url[:3])
                    + ("..." if len(missing_url) > 3 else "")
                )

            # Check for too many unverified papers
            unverified = [
                p.get("title", "?")
                for p in papers
                if p.get("verified") is False
            ]
            if unverified and len(unverified) > len(papers) * 0.5:
                issues.append(
                    f"{len(unverified)}/{len(papers)} papers are unverified — "
                    "too many uncertain citations"
                )

            # Minimum paper count
            if len(papers) < 5:
                issues.append(f"Only {len(papers)} papers cited (need 5+)")

        except yaml.YAMLError as e:
            issues.append(f"literature_map YAML is invalid: {e}")
        except Exception:
            pass
    return issues


def _check_implementation(
    state: ProjectState,
    sm: StateManager,
    pid: str,
    base_dir: Path,
) -> list[str]:
    """Check implementation artifacts for dummy data and fabrication."""
    issues: list[str] = []
    for art in _latest_artifacts_by_type(state, Stage.IMPLEMENTATION):
        if art.artifact_type != ArtifactType.CODE:
            continue
        try:
            content = sm.read_artifact_file(pid, art)
            lower = content.lower()
            if "dummydataset" in lower or "class dummydataset" in lower:
                issues.append("Code uses DummyDataset — must load real data")
            if "torch.randn" in lower and "dataset" in lower:
                issues.append(
                    "Dataset generates random tensors instead of loading real data"
                )
        except Exception:
            pass

    # Check if experiments/ has materialized code
    exp_dir = base_dir / "projects" / pid / "experiments"
    py_files = list(exp_dir.rglob("*.py")) if exp_dir.exists() else []

    # Check test_result for verified failures and fake passes
    for art in _latest_artifacts_by_type(state, Stage.IMPLEMENTATION):
        if art.artifact_type != ArtifactType.TEST_RESULT:
            continue
        try:
            content = yaml.safe_load(sm.read_artifact_file(pid, art))
            overall = str((content or {}).get("overall_status", "")).lower()
            verified = (content or {}).get("verified_by") == "orchestrator"

            # Hard fail: orchestrator-verified test_result shows failure
            if verified and overall in ("some_failed", "error", "failed"):
                issues.append(
                    f"VERIFIED test execution FAILED (overall_status: {overall}) — "
                    "code must be fixed before this stage can pass"
                )
            # Soft fail: claims passed but no code on disk
            elif "pass" in overall and not py_files:
                issues.append(
                    "test_result claims passed but experiments/ has no .py files — "
                    "tests may not have actually run"
                )
        except Exception:
            pass

    return issues


def _check_experimentation(
    state: ProjectState,
    sm: StateManager,
    pid: str,
    base_dir: Path,
) -> list[str]:
    """Check experimentation artifacts for materialized code and suspicious metrics."""
    issues: list[str] = []

    # Check code is materialized
    exp_dir = base_dir / "projects" / pid / "experiments"
    py_files = list(exp_dir.rglob("*.py")) if exp_dir.exists() else []
    if not py_files:
        issues.append("experiments/ has no .py files — code not materialized")

    # Check run_manifest smoke_test_command references existing script
    for art in _latest_artifacts_by_type(state, Stage.EXPERIMENTATION):
        if art.artifact_type != ArtifactType.RUN_MANIFEST:
            continue
        try:
            content = yaml.safe_load(sm.read_artifact_file(pid, art))
            smoke_cmd = (content or {}).get("smoke_test_command", "")
            if smoke_cmd:
                # Extract script path from command like "python experiments/foo.py"
                parts = smoke_cmd.split()
                for part in parts:
                    if part.endswith(".py"):
                        script_path = base_dir / "projects" / pid / part
                        if not script_path.exists():
                            issues.append(
                                f"smoke_test_command references '{part}' "
                                f"which does not exist"
                            )
                        break
        except Exception:
            pass

    # Check verified metrics for failure or placeholder
    for art in _latest_artifacts_by_type(state, Stage.EXPERIMENTATION):
        if art.artifact_type != ArtifactType.METRICS:
            continue
        try:
            content = yaml.safe_load(sm.read_artifact_file(pid, art))
            verified = (content or {}).get("verified_by") == "orchestrator"
            metrics = (content or {}).get("metrics_summary", [])

            # Hard fail: orchestrator-verified metrics are empty/placeholder
            if verified and metrics:
                placeholder = any(
                    m.get("name") == "no_metrics_parsed" for m in metrics
                )
                if placeholder:
                    issues.append(
                        "VERIFIED experiment produced no parseable metrics — "
                        "experiment may have failed or produced no output"
                    )

            # Hard fail: orchestrator-verified execution failed (exit != 0)
            if verified and (content or {}).get("execution_success") is False:
                exit_code = (content or {}).get("exit_code", "?")
                issues.append(
                    f"VERIFIED experiment execution FAILED (exit_code: {exit_code}) — "
                    "experiment must succeed before this stage can pass"
                )
            elif verified and (content or {}).get("raw_output_excerpt", "").startswith("TIMEOUT"):
                issues.append("VERIFIED experiment timed out")
        except Exception:
            pass

    # Check metrics aren't suspiciously perfect
    for art in _latest_artifacts_by_type(state, Stage.EXPERIMENTATION):
        if art.artifact_type != ArtifactType.METRICS:
            continue
        try:
            content = yaml.safe_load(sm.read_artifact_file(pid, art))
            metrics = (content or {}).get("metrics_summary", [])
            if metrics:
                all_just_pass = all(
                    m.get("current") is not None
                    and m.get("target") is not None
                    and 0
                    < (m["current"] - m["target"]) / max(abs(m["target"]), 0.001)
                    < 0.05
                    for m in metrics
                    if isinstance(m.get("current"), (int, float))
                    and isinstance(m.get("target"), (int, float))
                    and m.get("target") != 0
                )
                if all_just_pass and len(metrics) >= 3:
                    issues.append(
                        "All metrics barely exceed targets (within 5%) — "
                        "highly suspicious of fabrication"
                    )
        except Exception:
            pass

    return issues


def _check_analysis(
    state: ProjectState,
    sm: StateManager,
    pid: str,
    base_dir: Path,
) -> list[str]:
    """Check analysis artifacts for completeness and grounding."""
    issues: list[str] = []

    # Check experiments/ has code (analysis should be grounded in real runs)
    exp_dir = base_dir / "projects" / pid / "experiments"
    py_files = list(exp_dir.rglob("*.py")) if exp_dir.exists() else []
    if not py_files:
        issues.append(
            "experiments/ has no .py files — no real code was ever executed"
        )

    # Check claim_checklist completion
    for art in _latest_artifacts_by_type(state, Stage.ANALYSIS):
        if art.artifact_type != ArtifactType.CLAIM_CHECKLIST:
            continue
        try:
            content = yaml.safe_load(sm.read_artifact_file(pid, art))
            overall = (content or {}).get("overall_status", {})
            pct = overall.get("completion_percentage", "100%")
            if isinstance(pct, str):
                pct = float(pct.replace("%", ""))
            if pct < 50:
                issues.append(
                    f"Completion only {pct}% — most claims unvalidated"
                )
        except Exception:
            pass

    return issues


def get_completion_percentage(
    state: ProjectState,
    sm: StateManager,
    pid: str,
) -> Optional[float]:
    """Extract completion percentage from latest claim_checklist artifact.

    Used by verdict.evaluate_rollback to decide if analysis should trigger
    a backward transition.
    """
    for art in reversed(state.artifacts):
        if art.artifact_type == ArtifactType.CLAIM_CHECKLIST:
            try:
                content = yaml.safe_load(sm.read_artifact_file(pid, art))
                overall = (content or {}).get("overall_status", {})
                pct = overall.get("completion_percentage", "100%")
                if isinstance(pct, str):
                    pct = float(pct.replace("%", ""))
                return pct
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Backend capability verification
# ---------------------------------------------------------------------------

# Known backend limitations (tool → supported backends)
_BACKEND_CAPABILITIES: dict[str, set[str]] = {
    "WebSearch": {"claude"},
    "WebFetch": {"claude"},
    "allowedTools": {"claude"},  # Only claude supports --allowedTools isolation
}


def verify_backend_capabilities(
    backend: CLIBackend,
    role: AgentRole,
    stage: Stage,
) -> list[str]:
    """Check that the configured backend can do what the stage needs.

    Returns list of warnings (not blocking, but important to know).
    """
    warnings: list[str] = []

    # Researcher needs WebSearch for literature_review
    if (
        role == AgentRole.RESEARCHER
        and stage == Stage.LITERATURE_REVIEW
        and backend != CLIBackend.CLAUDE
    ):
        warnings.append(
            f"Researcher uses {backend.value} which lacks WebSearch/WebFetch. "
            f"Literature citations may be from training data only (not verified)."
        )

    # Critic should not use a backend that can write files
    if role == AgentRole.CRITIC and backend != CLIBackend.CODEX:
        warnings.append(
            f"Critic uses {backend.value} which can write files. "
            f"Critic may violate review-only constraint. "
            f"Recommended: use codex backend for Critic."
        )

    # Tool isolation only works with claude backend
    if backend != CLIBackend.CLAUDE and role in (
        AgentRole.RESEARCHER,
        AgentRole.ENGINEER,
    ):
        warnings.append(
            f"{role.value} uses {backend.value} which does not support "
            f"--allowedTools for tool isolation."
        )

    return warnings
