"""Artifact creation, validation, and schema enforcement.

Every stage produces typed artifacts. This module ensures they conform to
their YAML schema before being accepted by the gate system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pathlib import Path

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
    """Validate artifact YAML content against schema. Returns list of errors.

    Supported schema keys:
        required_fields   — top-level keys that must exist and be non-empty
        field_types        — expected type per field (string/list/number/mapping)
        min_lengths        — minimum list length per field
        min_string_lengths — minimum character count per string field
        list_item_fields   — required sub-fields for each item in a list field
        cross_field_checks — simple cross-field consistency rules
    """
    errors: list[str] = []
    if not schema:
        return errors  # No schema = no validation

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return [f"Invalid YAML: {e}"]

    if not isinstance(data, dict):
        return ["Artifact must be a YAML mapping"]

    # --- required_fields ---
    required_fields = schema.get("required_fields", [])
    for field in required_fields:
        if field not in data or data[field] is None or data[field] == "":
            errors.append(f"Missing required field: {field}")

    # --- field_types ---
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

    # --- min_lengths (list items) ---
    min_lengths = schema.get("min_lengths", {})
    for field_name, min_len in min_lengths.items():
        if field_name in data and isinstance(data[field_name], list):
            if len(data[field_name]) < min_len:
                errors.append(
                    f"Field '{field_name}' must have at least {min_len} items "
                    f"(has {len(data[field_name])})"
                )

    # --- min_string_lengths ---
    min_str_lens = schema.get("min_string_lengths", {})
    for field_name, min_chars in min_str_lens.items():
        val = data.get(field_name)
        if isinstance(val, str) and len(val) < min_chars:
            errors.append(
                f"Field '{field_name}' too short ({len(val)} chars, min {min_chars})"
            )

    # --- list_item_fields: required sub-fields in list items ---
    list_item_fields = schema.get("list_item_fields", {})
    for field_name, required_keys in list_item_fields.items():
        items = data.get(field_name)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(
                    f"'{field_name}[{i}]' must be a mapping, got {type(item).__name__}"
                )
                continue
            for key in required_keys:
                if key not in item or item[key] is None or item[key] == "":
                    errors.append(
                        f"'{field_name}[{i}]' missing required field '{key}'"
                    )

    # --- cross_field_checks: simple consistency rules ---
    cross_checks = schema.get("cross_field_checks", [])
    for check in cross_checks:
        rule = check.get("rule", "")
        if rule == "list_length_gte":
            # field_a length >= field_b length
            a_len = len(data.get(check["field_a"], []) or [])
            b_len = len(data.get(check["field_b"], []) or [])
            if a_len < b_len:
                errors.append(
                    f"Cross-field: '{check['field_a']}' ({a_len} items) must have "
                    f"at least as many items as '{check['field_b']}' ({b_len} items)"
                )
        elif rule == "field_not_empty_if":
            # field_a must be non-empty if field_b exists and is non-empty
            cond_val = data.get(check["field_b"])
            if cond_val and (cond_val is not None and cond_val != "" and cond_val != []):
                target = data.get(check["field_a"])
                if not target or target is None or target == "" or target == []:
                    errors.append(
                        f"Cross-field: '{check['field_a']}' must not be empty "
                        f"when '{check['field_b']}' is present"
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
    """Create an Artifact record (does not write file content — that's StateManager's job).

    IMPORTANT: The path is derived from artifact_type + computed version,
    NOT from the passed filename. This prevents version/path mismatches
    (e.g., state recording v2 pointing to _v1.yaml on disk).
    """
    # Determine version
    existing = [a for a in state.artifacts if a.artifact_type == artifact_type]
    version = max((a.version for a in existing), default=0) + 1

    # Canonical path: always derived from type + version (not passed filename)
    canonical = f"{artifact_type.value}_v{version}.yaml"
    rel_path = f"artifacts/{stage.value}/{canonical}"
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


def register_artifact_file(
    state: ProjectState,
    artifact_type: ArtifactType,
    stage: Stage,
    created_by: AgentRole,
    actual_path: Path,
    project_dir: Path,
    metadata: dict[str, Any] | None = None,
) -> Artifact:
    """Register an artifact file, renaming it to match the computed version.

    If the file on disk has a different name than the canonical version-based
    name, it will be renamed to prevent version/path mismatches in state.json.
    """
    existing = [a for a in state.artifacts if a.artifact_type == artifact_type]
    version = max((a.version for a in existing), default=0) + 1
    canonical = f"{artifact_type.value}_v{version}.yaml"

    # Compute expected canonical path on disk
    stage_dir = project_dir / "artifacts" / stage.value
    canonical_path = stage_dir / canonical

    # Rename if needed
    if actual_path.exists() and actual_path != canonical_path:
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        actual_path.rename(canonical_path)

    return create_artifact(state, artifact_type, stage, created_by, canonical, metadata)


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
