"""Shared fixtures for research_agent tests."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from research_agent.models import (
    AgentRole,
    ArtifactType,
    ProjectState,
    Stage,
)
from research_agent.state import StateManager


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    """Provide a temporary base directory for projects."""
    return tmp_path


@pytest.fixture
def state_manager(tmp_base: Path) -> StateManager:
    """Provide a StateManager backed by a tmp dir."""
    return StateManager(tmp_base)


@pytest.fixture
def sample_project(state_manager: StateManager) -> ProjectState:
    """Create a sample project and return its state."""
    return state_manager.create_project(
        name="Test Project",
        description="A test project",
        research_question="Does X cause Y?",
    )


@pytest.fixture
def schema_dir(tmp_base: Path) -> Path:
    """Provide a temporary schema directory with sample schemas."""
    d = tmp_base / "schemas"
    d.mkdir(parents=True, exist_ok=True)
    return d
