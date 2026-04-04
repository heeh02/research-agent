"""Artifact creation, validation, and schema enforcement.

Every stage produces typed artifacts. This module ensures they conform to
their YAML schema before being accepted by the gate system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import AgentRole, Artifact, ArtifactType, ProjectState, Stage


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[str, dict] = {}


def load_schema(schema_dir: Path, artifact_type: ArtifactType) -> dict[str, Any]:
    """Load the YAML schema for an artifact type."""
    key = artifact_type.value
    if key not in _SCHEMA_CACHE:
        schema_file = schema_dir / f"{key}.schema.yaml"
        if schema_file.exists():
            _SCHEMA_CACHE[key] = yaml.safe_load(schema_file.read_text(encoding="utf-8"))
        else:
            _SCHEMA_CACHE[key] = {}
    return _SCHEMA_CACHE[key]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_artifact_content(content: str, schema: dict[str, Any]) -> list[str]:
    """Validate artifact YAML content against schema. Returns list of errors."""
    errors: list[str] = []
    if not schema:
        return errors  # No schema = no validation

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return [f"Invalid YAML: {e}"]

    if not isinstance(data, dict):
        return ["Artifact must be a YAML mapping"]

    required_fields = schema.get("required_fields", [])
    for field in required_fields:
        if field not in data or data[field] is None or data[field] == "":
            errors.append(f"Missing required field: {field}")

    # Check field types if specified
    field_types = schema.get("field_types", {})
    for field_name, expected_type in field_types.items():
        if field_name in data and data[field_name] is not None:
            if expected_type == "list" and not isinstance(data[field_name], list):
                errors.append(f"Field '{field_name}' must be a list")
            elif expected_type == "string" and not isinstance(data[field_name], str):
                errors.append(f"Field '{field_name}' must be a string")
            elif expected_type == "number" and not isinstance(data[field_name], (int, float)):
                errors.append(f"Field '{field_name}' must be a number")
            elif expected_type == "mapping" and not isinstance(data[field_name], dict):
                errors.append(f"Field '{field_name}' must be a mapping")

    # Check minimum list lengths
    min_lengths = schema.get("min_lengths", {})
    for field_name, min_len in min_lengths.items():
        if field_name in data and isinstance(data[field_name], list):
            if len(data[field_name]) < min_len:
                errors.append(
                    f"Field '{field_name}' must have at least {min_len} items "
                    f"(has {len(data[field_name])})"
                )

    return errors


# ---------------------------------------------------------------------------
# Artifact creation
# ---------------------------------------------------------------------------

def create_artifact(
    state: ProjectState,
    artifact_type: ArtifactType,
    stage: Stage,
    created_by: AgentRole,
    filename: str,
    metadata: dict[str, Any] | None = None,
) -> Artifact:
    """Create an Artifact record (does not write file content — that's StateManager's job)."""
    # Determine version
    existing = [a for a in state.artifacts if a.artifact_type == artifact_type]
    version = max((a.version for a in existing), default=0) + 1

    rel_path = f"artifacts/{stage.value}/{filename}"
    artifact = Artifact(
        name=f"{artifact_type.value}_v{version}",
        artifact_type=artifact_type,
        stage=stage,
        version=version,
        path=rel_path,
        created_by=created_by,
        metadata=metadata or {},
    )
    state.artifacts.append(artifact)
    return artifact


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def assemble_context(
    state: ProjectState,
    artifact_contents: dict[ArtifactType, str],
    target_stage: Stage,
) -> str:
    """Build a context string for an agent, including relevant artifacts.

    This is the key function that eliminates manual copy-paste between tools.
    It assembles everything an agent needs to know to do its job at a given stage.
    """
    parts: list[str] = []

    # Project header
    parts.append(f"# Project: {state.name}")
    parts.append(f"Research Question: {state.research_question}")
    parts.append(f"Current Stage: {target_stage.value}")
    parts.append(f"Iteration: {state.iteration_count.get(target_stage.value, 1)}")
    parts.append("")

    # Previous gate feedback (if any — crucial for iterations)
    stage_gates = [g for g in state.gate_results if g.stage == target_stage]
    if stage_gates:
        latest_gate = stage_gates[-1]
        if latest_gate.overall_feedback:
            parts.append("## Previous Review Feedback")
            parts.append(latest_gate.overall_feedback)
            parts.append("")
            for check in latest_gate.checks:
                if not check.passed:
                    parts.append(f"- FAILED: {check.name} — {check.feedback}")
            parts.append("")

    # Relevant artifacts
    if artifact_contents:
        parts.append("## Relevant Artifacts")
        for atype, content in artifact_contents.items():
            parts.append(f"\n### {atype.value}")
            parts.append("```yaml")
            parts.append(content.strip())
            parts.append("```")
        parts.append("")

    # Cost summary
    total = state.total_cost()
    if total > 0:
        parts.append(f"## Cost So Far: ${total:.4f}")
        parts.append("")

    return "\n".join(parts)
