"""Tests for research_agent.artifacts — schema loading, validation, artifact creation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_agent.artifacts import (
    assemble_context,
    create_artifact,
    load_schema,
    register_artifact_file,
    validate_artifact_content,
)
from research_agent.models import (
    AgentRole,
    Artifact,
    ArtifactType,
    GateCheck,
    GateResult,
    GateStatus,
    ProjectState,
    Stage,
)


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

class TestLoadSchema:
    def test_load_existing_schema(self, schema_dir: Path):
        schema_content = yaml.dump({
            "required_fields": ["title", "summary"],
            "field_types": {"title": "string", "summary": "string"},
        })
        (schema_dir / "problem_brief.schema.yaml").write_text(schema_content)
        schema = load_schema(schema_dir, ArtifactType.PROBLEM_BRIEF)
        assert "required_fields" in schema
        assert "title" in schema["required_fields"]

    def test_load_nonexistent_schema(self, tmp_path: Path):
        # Ensure cache doesn't interfere: use a unique schema_dir
        schema = load_schema(tmp_path / "empty_schemas", ArtifactType.LITERATURE_MAP)
        assert schema == {}

    def test_schema_caching(self, schema_dir: Path):
        (schema_dir / "hypothesis_card.schema.yaml").write_text(
            yaml.dump({"required_fields": ["hypothesis"]})
        )
        s1 = load_schema(schema_dir, ArtifactType.HYPOTHESIS_CARD)
        s2 = load_schema(schema_dir, ArtifactType.HYPOTHESIS_CARD)
        assert s1 is s2  # Same object from cache


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_content(self):
        schema = {
            "required_fields": ["title", "summary"],
            "field_types": {"title": "string", "summary": "string"},
        }
        content = yaml.dump({"title": "Hello", "summary": "World"})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_missing_required_field(self):
        schema = {"required_fields": ["title", "summary"]}
        content = yaml.dump({"title": "Hello"})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1
        assert "summary" in errors[0]

    def test_empty_required_field(self):
        schema = {"required_fields": ["title"]}
        content = yaml.dump({"title": ""})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1

    def test_none_required_field(self):
        schema = {"required_fields": ["title"]}
        content = yaml.dump({"title": None})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1

    def test_wrong_field_type_list(self):
        schema = {"field_types": {"tags": "list"}}
        content = yaml.dump({"tags": "not a list"})
        errors = validate_artifact_content(content, schema)
        assert any("list" in e for e in errors)

    def test_wrong_field_type_string(self):
        schema = {"field_types": {"name": "string"}}
        content = yaml.dump({"name": 42})
        errors = validate_artifact_content(content, schema)
        assert any("string" in e for e in errors)

    def test_wrong_field_type_number(self):
        schema = {"field_types": {"count": "number"}}
        content = yaml.dump({"count": "not a number"})
        errors = validate_artifact_content(content, schema)
        assert any("number" in e for e in errors)

    def test_wrong_field_type_mapping(self):
        schema = {"field_types": {"config": "mapping"}}
        content = yaml.dump({"config": [1, 2, 3]})
        errors = validate_artifact_content(content, schema)
        assert any("mapping" in e for e in errors)

    def test_min_lengths_pass(self):
        schema = {"min_lengths": {"items": 2}}
        content = yaml.dump({"items": [1, 2, 3]})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_min_lengths_fail(self):
        schema = {"min_lengths": {"items": 3}}
        content = yaml.dump({"items": [1]})
        errors = validate_artifact_content(content, schema)
        assert any("at least 3" in e for e in errors)

    def test_invalid_yaml(self):
        schema = {"required_fields": ["title"]}
        errors = validate_artifact_content("not: valid: yaml: {{{", schema)
        assert any("Invalid YAML" in e for e in errors)

    def test_non_mapping_yaml(self):
        schema = {"required_fields": ["title"]}
        content = "- item1\n- item2"
        errors = validate_artifact_content(content, schema)
        assert any("mapping" in e for e in errors)

    def test_empty_schema(self):
        errors = validate_artifact_content("anything: works", {})
        assert errors == []

    def test_none_value_skips_type_check(self):
        schema = {"field_types": {"optional_field": "string"}}
        content = yaml.dump({"optional_field": None})
        errors = validate_artifact_content(content, schema)
        assert errors == []


# ---------------------------------------------------------------------------
# Artifact creation
# ---------------------------------------------------------------------------

class TestCreateArtifact:
    def test_first_version(self):
        ps = ProjectState(project_id="t", name="T")
        art = create_artifact(
            ps, ArtifactType.PROBLEM_BRIEF, Stage.PROBLEM_DEFINITION,
            AgentRole.RESEARCHER, "problem_brief_v1.yaml",
        )
        assert art.version == 1
        assert art.path == "artifacts/problem_definition/problem_brief_v1.yaml"
        assert art in ps.artifacts

    def test_version_auto_increment(self):
        ps = ProjectState(project_id="t", name="T")
        create_artifact(
            ps, ArtifactType.PROBLEM_BRIEF, Stage.PROBLEM_DEFINITION,
            AgentRole.RESEARCHER, "pb_v1.yaml",
        )
        art2 = create_artifact(
            ps, ArtifactType.PROBLEM_BRIEF, Stage.PROBLEM_DEFINITION,
            AgentRole.RESEARCHER, "pb_v2.yaml",
        )
        assert art2.version == 2
        assert "v2" in art2.path

    def test_canonical_path_ignores_filename(self):
        """Path is derived from type+version, not the passed filename."""
        ps = ProjectState(project_id="t", name="T")
        art = create_artifact(
            ps, ArtifactType.HYPOTHESIS_CARD, Stage.HYPOTHESIS_FORMATION,
            AgentRole.RESEARCHER, "whatever_name.yaml",
        )
        assert art.path == "artifacts/hypothesis_formation/hypothesis_card_v1.yaml"


class TestRegisterArtifactFile:
    def test_register_and_rename(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        stage_dir = project_dir / "artifacts" / "problem_definition"
        stage_dir.mkdir(parents=True)

        # Create a file with a non-canonical name
        actual = stage_dir / "draft_brief.yaml"
        actual.write_text("title: Draft")

        ps = ProjectState(project_id="t", name="T")
        art = register_artifact_file(
            ps, ArtifactType.PROBLEM_BRIEF, Stage.PROBLEM_DEFINITION,
            AgentRole.RESEARCHER, actual, project_dir,
        )
        # File should be renamed to canonical
        canonical = stage_dir / "problem_brief_v1.yaml"
        assert canonical.exists()
        assert not actual.exists()
        assert art.version == 1


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

class TestAssembleContext:
    def test_basic_context(self):
        ps = ProjectState(
            project_id="t", name="My Project",
            research_question="Does X cause Y?",
        )
        ctx = assemble_context(ps, {}, Stage.PROBLEM_DEFINITION)
        assert "My Project" in ctx
        assert "Does X cause Y?" in ctx
        assert "problem_definition" in ctx

    def test_context_with_artifacts(self):
        ps = ProjectState(project_id="t", name="T", research_question="Q")
        artifacts = {
            ArtifactType.PROBLEM_BRIEF: "title: Test Brief\nsummary: A summary",
        }
        ctx = assemble_context(ps, artifacts, Stage.PROBLEM_DEFINITION)
        assert "problem_brief" in ctx
        assert "Test Brief" in ctx

    def test_context_with_feedback(self):
        ps = ProjectState(project_id="t", name="T", research_question="Q")
        ps.gate_results.append(GateResult(
            gate_name="test", stage=Stage.PROBLEM_DEFINITION,
            status=GateStatus.FAILED,
            overall_feedback="Needs more detail",
            checks=[GateCheck(
                name="completeness", description="", check_type="ai_eval",
                passed=False, feedback="Too vague",
            )],
        ))
        ctx = assemble_context(ps, {}, Stage.PROBLEM_DEFINITION)
        assert "Needs more detail" in ctx
        assert "Too vague" in ctx

    def test_context_with_cost(self):
        from research_agent.models import CostRecord, LLMProvider
        ps = ProjectState(project_id="t", name="T", research_question="Q")
        ps.cost_records.append(CostRecord(
            agent=AgentRole.RESEARCHER, provider=LLMProvider.CLAUDE,
            model="s", input_tokens=100, output_tokens=50,
            cost_usd=0.01, task_description="t", stage=Stage.PROBLEM_DEFINITION,
        ))
        ctx = assemble_context(ps, {}, Stage.PROBLEM_DEFINITION)
        assert "$0.01" in ctx


# ---------------------------------------------------------------------------
# Enhanced validation — min_string_lengths, list_item_fields, cross_field_checks
# ---------------------------------------------------------------------------

class TestMinStringLengths:
    def test_string_too_short(self):
        schema = {"min_string_lengths": {"claim": 30}}
        content = yaml.dump({"claim": "short"})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1
        assert "too short" in errors[0]
        assert "5 chars" in errors[0]

    def test_string_long_enough(self):
        schema = {"min_string_lengths": {"claim": 5}}
        content = yaml.dump({"claim": "This is long enough"})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_missing_field_skipped(self):
        """min_string_lengths should not error if field is absent (required_fields handles that)."""
        schema = {"min_string_lengths": {"claim": 30}}
        content = yaml.dump({"other": "value"})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_non_string_field_skipped(self):
        schema = {"min_string_lengths": {"count": 5}}
        content = yaml.dump({"count": 42})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_trivial_placeholder_rejected(self):
        """The core bug: 'x' should not pass a claim field with min 30 chars."""
        schema = {
            "required_fields": ["claim"],
            "field_types": {"claim": "string"},
            "min_string_lengths": {"claim": 30},
        }
        content = yaml.dump({"claim": "x"})
        errors = validate_artifact_content(content, schema)
        assert any("too short" in e for e in errors)


class TestListItemFields:
    def test_items_with_required_subfields(self):
        schema = {"list_item_fields": {"papers": ["title", "url"]}}
        content = yaml.dump({"papers": [
            {"title": "Paper A", "url": "https://example.com"},
            {"title": "Paper B", "url": "https://example.com"},
        ]})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_item_missing_subfield(self):
        schema = {"list_item_fields": {"papers": ["title", "url"]}}
        content = yaml.dump({"papers": [
            {"title": "Paper A"},  # missing url
        ]})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1
        assert "'url'" in errors[0]

    def test_item_empty_subfield(self):
        schema = {"list_item_fields": {"papers": ["title", "url"]}}
        content = yaml.dump({"papers": [
            {"title": "Paper A", "url": ""},
        ]})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1
        assert "'url'" in errors[0]

    def test_item_not_a_mapping(self):
        schema = {"list_item_fields": {"papers": ["title"]}}
        content = yaml.dump({"papers": ["just a string"]})
        errors = validate_artifact_content(content, schema)
        assert any("must be a mapping" in e for e in errors)

    def test_missing_list_field_skipped(self):
        schema = {"list_item_fields": {"papers": ["title"]}}
        content = yaml.dump({"other": "value"})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_multiple_items_multiple_errors(self):
        schema = {"list_item_fields": {"files": ["path", "content"]}}
        content = yaml.dump({"files": [
            {"path": "a.py"},            # missing content
            {"content": "print('hi')"},  # missing path
        ]})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 2

    def test_code_artifact_must_have_path_and_content(self):
        """Regression: code artifact files without 'content' should fail."""
        schema = {
            "required_fields": ["files"],
            "field_types": {"files": "list"},
            "min_lengths": {"files": 1},
            "list_item_fields": {"files": ["path", "content"]},
        }
        # Old schema would pass this — only checked list length
        content = yaml.dump({"files": [{"path": "train.py"}]})
        errors = validate_artifact_content(content, schema)
        assert any("'content'" in e for e in errors)


class TestCrossFieldChecks:
    def test_list_length_gte_pass(self):
        schema = {"cross_field_checks": [
            {"rule": "list_length_gte", "field_a": "kill_criteria", "field_b": "predictions"},
        ]}
        content = yaml.dump({
            "kill_criteria": ["a", "b", "c"],
            "predictions": ["x", "y"],
        })
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_list_length_gte_fail(self):
        schema = {"cross_field_checks": [
            {"rule": "list_length_gte", "field_a": "kill_criteria", "field_b": "predictions"},
        ]}
        content = yaml.dump({
            "kill_criteria": ["a"],
            "predictions": ["x", "y", "z"],
        })
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1
        assert "kill_criteria" in errors[0]

    def test_field_not_empty_if_pass(self):
        schema = {"cross_field_checks": [
            {"rule": "field_not_empty_if", "field_a": "mitigation", "field_b": "risk"},
        ]}
        content = yaml.dump({"risk": "high", "mitigation": "use fallback"})
        errors = validate_artifact_content(content, schema)
        assert errors == []

    def test_field_not_empty_if_fail(self):
        schema = {"cross_field_checks": [
            {"rule": "field_not_empty_if", "field_a": "mitigation", "field_b": "risk"},
        ]}
        content = yaml.dump({"risk": "high", "mitigation": ""})
        errors = validate_artifact_content(content, schema)
        assert len(errors) == 1

    def test_unknown_rule_ignored(self):
        schema = {"cross_field_checks": [
            {"rule": "nonexistent_rule", "field_a": "a", "field_b": "b"},
        ]}
        content = yaml.dump({"a": 1, "b": 2})
        errors = validate_artifact_content(content, schema)
        assert errors == []


class TestRealSchemaRegressions:
    """Test with actual upgraded schema files to prove old-weak inputs now fail."""

    def test_literature_map_paper_without_url_rejected(self):
        """Previously: paper with just title would pass. Now: url is required."""
        schema = {
            "required_fields": ["papers"],
            "field_types": {"papers": "list"},
            "min_lengths": {"papers": 1},
            "list_item_fields": {"papers": ["title", "url"]},
        }
        content = yaml.dump({"papers": [
            {"title": "Some Paper", "authors": "Smith et al.", "year": 2024},
        ]})
        errors = validate_artifact_content(content, schema)
        assert any("'url'" in e for e in errors)

    def test_hypothesis_card_trivial_claim_rejected(self):
        """Previously: claim='x' would pass. Now: min 30 chars."""
        schema = {
            "required_fields": ["claim"],
            "field_types": {"claim": "string"},
            "min_string_lengths": {"claim": 30},
        }
        content = yaml.dump({"claim": "x"})
        errors = validate_artifact_content(content, schema)
        assert any("too short" in e for e in errors)

    def test_code_file_without_content_rejected(self):
        """Previously: files=[{path:'a.py'}] would pass. Now: content is required."""
        schema = {
            "required_fields": ["files"],
            "field_types": {"files": "list"},
            "list_item_fields": {"files": ["path", "content"]},
        }
        content = yaml.dump({"files": [{"path": "train.py"}]})
        errors = validate_artifact_content(content, schema)
        assert any("'content'" in e for e in errors)

    def test_metrics_entry_without_current_rejected(self):
        """Previously: metrics_summary=[{name:'acc'}] would pass. Now: current required."""
        schema = {
            "required_fields": ["metrics_summary"],
            "field_types": {"metrics_summary": "list"},
            "list_item_fields": {"metrics_summary": ["name", "current"]},
        }
        content = yaml.dump({"metrics_summary": [{"name": "accuracy"}]})
        errors = validate_artifact_content(content, schema)
        assert any("'current'" in e for e in errors)
