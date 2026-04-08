"""Tests for research_agent.sandbox — workspace snapshot and violation detection."""
from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.models import AgentRole, Stage
from research_agent.sandbox import (
    FileSnapshot,
    Violation,
    ViolationReport,
    check_violations,
    snapshot_directory,
    _allowed_write_patterns,
    _matches_any_pattern,
)


# ---------------------------------------------------------------------------
# snapshot_directory
# ---------------------------------------------------------------------------

class TestSnapshotDirectory:
    def test_snapshot_empty_project(self, tmp_path: Path):
        project_dir = tmp_path / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        snap = snapshot_directory(tmp_path, "proj1")
        assert snap.files == {}

    def test_snapshot_with_files(self, tmp_path: Path):
        project_dir = tmp_path / "projects" / "proj1"
        art_dir = project_dir / "artifacts" / "problem_definition"
        art_dir.mkdir(parents=True)
        (art_dir / "problem_brief_v1.yaml").write_text("title: Test")
        (project_dir / "logs").mkdir()
        (project_dir / "logs" / "run.txt").write_text("log")

        snap = snapshot_directory(tmp_path, "proj1")
        # Paths are now relative to base_dir, not project_dir
        assert "projects/proj1/artifacts/problem_definition/problem_brief_v1.yaml" in snap.files
        assert "projects/proj1/logs/run.txt" in snap.files

    def test_snapshot_excludes_state_json(self, tmp_path: Path):
        project_dir = tmp_path / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        (project_dir / "state.json").write_text("{}")

        snap = snapshot_directory(tmp_path, "proj1")
        assert "state.json" not in snap.files

    def test_snapshot_nonexistent_project(self, tmp_path: Path):
        snap = snapshot_directory(tmp_path, "nonexistent")
        assert snap.files == {}


# ---------------------------------------------------------------------------
# _allowed_write_patterns
# ---------------------------------------------------------------------------

class TestAllowedWritePatterns:
    def test_researcher_only_artifacts(self):
        patterns = _allowed_write_patterns(AgentRole.RESEARCHER, Stage.PROBLEM_DEFINITION, "proj1")
        assert patterns == ["projects/proj1/artifacts/problem_definition/"]

    def test_engineer_artifacts_and_experiments(self):
        patterns = _allowed_write_patterns(AgentRole.ENGINEER, Stage.IMPLEMENTATION, "proj1")
        assert "projects/proj1/artifacts/implementation/" in patterns
        assert "projects/proj1/experiments/" in patterns

    def test_critic_very_restricted(self):
        patterns = _allowed_write_patterns(AgentRole.CRITIC, Stage.PROBLEM_DEFINITION, "proj1")
        # Critic can only write review files
        assert len(patterns) == 1
        assert "review_" in patterns[0]

    def test_orchestrator_artifacts_and_logs(self):
        patterns = _allowed_write_patterns(AgentRole.ORCHESTRATOR, Stage.IMPLEMENTATION, "proj1")
        assert "projects/proj1/artifacts/implementation/" in patterns
        assert "projects/proj1/logs/" in patterns

    def test_no_project_id_backward_compat(self):
        patterns = _allowed_write_patterns(AgentRole.RESEARCHER, Stage.PROBLEM_DEFINITION)
        assert patterns == ["artifacts/problem_definition/"]


# ---------------------------------------------------------------------------
# _matches_any_pattern
# ---------------------------------------------------------------------------

class TestMatchesPattern:
    def test_match(self):
        assert _matches_any_pattern("artifacts/problem_definition/brief.yaml",
                                     ["artifacts/problem_definition/"])

    def test_no_match(self):
        assert not _matches_any_pattern("experiments/train.py",
                                         ["artifacts/problem_definition/"])

    def test_multiple_patterns(self):
        patterns = ["artifacts/implementation/", "experiments/"]
        assert _matches_any_pattern("experiments/train.py", patterns)
        assert _matches_any_pattern("artifacts/implementation/code.yaml", patterns)
        assert not _matches_any_pattern("artifacts/hypothesis_formation/h.yaml", patterns)


# ---------------------------------------------------------------------------
# check_violations
# ---------------------------------------------------------------------------

class TestCheckViolations:
    """All snapshot paths are relative to base_dir (e.g., projects/proj1/...)."""

    def _make_snapshots(self) -> tuple[FileSnapshot, FileSnapshot]:
        before = FileSnapshot(files={
            "projects/proj1/artifacts/problem_definition/brief_v1.yaml": 1000.0,
        })
        after = FileSnapshot(files={
            "projects/proj1/artifacts/problem_definition/brief_v1.yaml": 1000.0,
            "projects/proj1/artifacts/problem_definition/brief_v2.yaml": 2000.0,
        })
        return before, after

    def test_no_violations_expected_output(self):
        before, after = self._make_snapshots()
        report = check_violations(
            before, after, AgentRole.RESEARCHER, Stage.PROBLEM_DEFINITION,
            expected_outputs=["projects/proj1/artifacts/problem_definition/brief_v2.yaml"],
            project_id="proj1",
        )
        assert report.clean

    def test_no_violations_allowed_pattern(self):
        before, after = self._make_snapshots()
        report = check_violations(
            before, after, AgentRole.RESEARCHER, Stage.PROBLEM_DEFINITION,
            expected_outputs=[],
            project_id="proj1",
        )
        assert report.clean

    def test_violation_wrong_stage_dir(self):
        before = FileSnapshot(files={})
        after = FileSnapshot(files={
            "projects/proj1/artifacts/implementation/code_v1.yaml": 2000.0,
        })
        report = check_violations(
            before, after, AgentRole.RESEARCHER, Stage.PROBLEM_DEFINITION,
            expected_outputs=[],
            project_id="proj1",
        )
        assert not report.clean
        assert len(report.violations) == 1
        assert report.violations[0].kind == "created"

    def test_violation_critic_writes_file(self):
        before = FileSnapshot(files={})
        after = FileSnapshot(files={
            "projects/proj1/artifacts/problem_definition/problem_brief_v2.yaml": 2000.0,
        })
        report = check_violations(
            before, after, AgentRole.CRITIC, Stage.PROBLEM_DEFINITION,
            expected_outputs=[],
            project_id="proj1",
        )
        assert not report.clean
        assert len(report.violations) == 1

    def test_critic_review_file_allowed(self):
        before = FileSnapshot(files={})
        after = FileSnapshot(files={
            "projects/proj1/artifacts/problem_definition/review_task123.yaml": 2000.0,
        })
        report = check_violations(
            before, after, AgentRole.CRITIC, Stage.PROBLEM_DEFINITION,
            expected_outputs=[],
            project_id="proj1",
        )
        assert report.clean

    def test_violation_modified_file(self):
        before = FileSnapshot(files={
            "projects/proj1/artifacts/hypothesis_formation/hypothesis_card_v1.yaml": 1000.0,
        })
        after = FileSnapshot(files={
            "projects/proj1/artifacts/hypothesis_formation/hypothesis_card_v1.yaml": 2000.0,
        })
        report = check_violations(
            before, after, AgentRole.ENGINEER, Stage.IMPLEMENTATION,
            expected_outputs=[],
            project_id="proj1",
        )
        assert not report.clean
        assert report.violations[0].kind == "modified"

    def test_engineer_experiments_allowed(self):
        before = FileSnapshot(files={})
        after = FileSnapshot(files={
            "projects/proj1/experiments/train.py": 2000.0,
            "projects/proj1/experiments/configs/main.yaml": 2000.0,
        })
        report = check_violations(
            before, after, AgentRole.ENGINEER, Stage.IMPLEMENTATION,
            expected_outputs=[],
            project_id="proj1",
        )
        assert report.clean

    def test_expected_output_overrides_pattern(self):
        """Expected outputs are always allowed, even if outside normal patterns."""
        before = FileSnapshot(files={})
        after = FileSnapshot(files={
            "projects/proj1/some/unusual/path.yaml": 2000.0,
        })
        report = check_violations(
            before, after, AgentRole.RESEARCHER, Stage.PROBLEM_DEFINITION,
            expected_outputs=["projects/proj1/some/unusual/path.yaml"],
            project_id="proj1",
        )
        assert report.clean

    def test_unchanged_files_ignored(self):
        before = FileSnapshot(files={
            "projects/proj1/artifacts/problem_definition/old.yaml": 1000.0,
            "projects/proj1/experiments/train.py": 1000.0,
        })
        after = FileSnapshot(files={
            "projects/proj1/artifacts/problem_definition/old.yaml": 1000.0,
            "projects/proj1/experiments/train.py": 1000.0,
        })
        report = check_violations(
            before, after, AgentRole.CRITIC, Stage.PROBLEM_DEFINITION,
            expected_outputs=[], project_id="proj1",
        )
        assert report.clean

    def test_protected_dir_write_is_violation(self):
        """Agent writing to config/ is detected as a violation."""
        before = FileSnapshot(files={
            "config/settings.yaml": 1000.0,
        })
        after = FileSnapshot(files={
            "config/settings.yaml": 2000.0,  # Modified!
        })
        report = check_violations(
            before, after, AgentRole.ENGINEER, Stage.IMPLEMENTATION,
            expected_outputs=[], project_id="proj1",
        )
        assert not report.clean
        assert report.violations[0].path == "config/settings.yaml"

    def test_snapshot_protected_directories(self, tmp_path: Path):
        """Protected dirs (config/, agents/) appear in the snapshot."""
        project_dir = tmp_path / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "settings.yaml").write_text("key: val")
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "researcher").mkdir(parents=True)
        (tmp_path / "agents" / "researcher" / "CLAUDE.md").write_text("# instructions")

        snap = snapshot_directory(tmp_path, "proj1")
        assert "config/settings.yaml" in snap.files
        assert "agents/researcher/CLAUDE.md" in snap.files

    def test_scripts_dir_write_is_violation(self, tmp_path: Path):
        """Agent writing to scripts/ is detected as a violation."""
        import time as _time
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "multi_agent.py").write_text("original")

        before = snapshot_directory(tmp_path, "proj1")
        # Ensure mtime separation exceeds the 0.01s threshold in check_violations
        _time.sleep(0.05)
        (tmp_path / "scripts" / "multi_agent.py").write_text("compromised")

        after = snapshot_directory(tmp_path, "proj1")
        report = check_violations(
            before, after, AgentRole.ENGINEER, Stage.IMPLEMENTATION,
            expected_outputs=[], project_id="proj1",
        )
        assert not report.clean
        assert any("scripts/multi_agent.py" in v.path for v in report.violations)

    def test_root_control_file_write_is_violation(self, tmp_path: Path):
        """Agent modifying pyproject.toml is detected as a violation."""
        import time as _time
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'original'")

        before = snapshot_directory(tmp_path, "proj1")
        _time.sleep(0.05)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'compromised'")

        after = snapshot_directory(tmp_path, "proj1")
        report = check_violations(
            before, after, AgentRole.ENGINEER, Stage.IMPLEMENTATION,
            expected_outputs=[], project_id="proj1",
        )
        assert not report.clean
        assert any("pyproject.toml" in v.path for v in report.violations)

    def test_root_toml_in_snapshot(self, tmp_path: Path):
        """Root-level .toml, .yaml, .md files appear in the snapshot."""
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text("[project]")
        (tmp_path / "settings.yaml").write_text("key: val")
        (tmp_path / "CLAUDE.md").write_text("# instructions")

        snap = snapshot_directory(tmp_path, "proj1")
        assert "pyproject.toml" in snap.files
        assert "settings.yaml" in snap.files
        assert "CLAUDE.md" in snap.files

    def test_nested_codex_in_snapshot(self, tmp_path: Path):
        """Nested .codex/ paths are scanned recursively."""
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        codex_dir = tmp_path / ".codex" / "nested"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text("[settings]")

        snap = snapshot_directory(tmp_path, "proj1")
        assert ".codex/nested/config.toml" in snap.files

    def test_top_level_experiments_dir_monitored(self, tmp_path: Path):
        """Top-level experiments/ dir (outside projects/) is monitored."""
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        (tmp_path / "experiments").mkdir()
        (tmp_path / "experiments" / "seed.py").write_text("import torch")

        snap = snapshot_directory(tmp_path, "proj1")
        assert "experiments/seed.py" in snap.files

    def test_unknown_top_level_dir_monitored(self, tmp_path: Path):
        """Any new top-level directory is automatically included."""
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        (tmp_path / "newdir").mkdir()
        (tmp_path / "newdir" / "data.csv").write_text("a,b")

        snap = snapshot_directory(tmp_path, "proj1")
        assert "newdir/data.csv" in snap.files

    def test_other_project_excluded(self, tmp_path: Path):
        """Files in other projects are NOT included in the snapshot."""
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        other = tmp_path / "projects" / "other"
        other.mkdir(parents=True)
        (other / "artifacts" / "problem_definition").mkdir(parents=True)
        (other / "artifacts" / "problem_definition" / "brief.yaml").write_text("x: 1")

        snap = snapshot_directory(tmp_path, "proj1")
        assert not any("other" in k for k in snap.files)

    def test_git_dir_excluded(self, tmp_path: Path):
        """The .git directory is never scanned."""
        (tmp_path / "projects" / "proj1").mkdir(parents=True)
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "objects" / "pack").write_text("binary")

        snap = snapshot_directory(tmp_path, "proj1")
        assert not any(".git" in k for k in snap.files)

    def test_pyc_excluded_from_protected_scan(self, tmp_path: Path):
        """Python bytecode excluded from protected dir snapshots."""
        project_dir = tmp_path / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        pycache = tmp_path / "src" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "module.cpython-311.pyc").write_bytes(b"bytecode")
        (tmp_path / "src" / "real.py").write_text("print('hi')")

        snap = snapshot_directory(tmp_path, "proj1")
        assert "src/real.py" in snap.files
        pyc_files = [k for k in snap.files if k.endswith(".pyc")]
        assert pyc_files == []


# ---------------------------------------------------------------------------
# ViolationReport
# ---------------------------------------------------------------------------

class TestViolationReport:
    def test_clean_report(self):
        r = ViolationReport(role=AgentRole.RESEARCHER, stage=Stage.PROBLEM_DEFINITION)
        assert r.clean
        assert "no violations" in r.summary()

    def test_dirty_report(self):
        r = ViolationReport(
            role=AgentRole.CRITIC, stage=Stage.PROBLEM_DEFINITION,
            violations=[Violation(
                path="artifacts/problem_definition/v2.yaml",
                kind="created",
                role=AgentRole.CRITIC,
                stage=Stage.PROBLEM_DEFINITION,
            )],
        )
        assert not r.clean
        assert "1 violation" in r.summary()
        assert "v2.yaml" in r.summary()
