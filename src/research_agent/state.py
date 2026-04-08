"""Project state persistence — file-based, JSON-serialized.

Each project lives in its own directory under `projects/`.
State is written atomically (tmp + rename) and protected against
concurrent updates via file locking (fcntl.flock).
"""

from __future__ import annotations

import fcntl
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Artifact, ArtifactType, ProjectState, Stage


class StateManager:
    """Manages project lifecycle and persistence."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.projects_dir = base_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def create_project(self, name: str, description: str = "",
                       research_question: str = "") -> ProjectState:
        project_id = f"{_slugify(name)}-{uuid.uuid4().hex[:8]}"
        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True)

        # Create stage directories
        for stage in Stage:
            (project_dir / "artifacts" / stage.value).mkdir(parents=True)
        (project_dir / "logs").mkdir()
        (project_dir / "experiments").mkdir()

        state = ProjectState(
            project_id=project_id,
            name=name,
            description=description,
            research_question=research_question,
        )
        self._save_state(state)
        return state

    def load_project(self, project_id: str) -> ProjectState:
        """Load project state with shared lock to avoid reading during a write."""
        state_file = self.projects_dir / project_id / "state.json"
        if not state_file.exists():
            raise FileNotFoundError(f"Project not found: {project_id}")

        lock_file = self._lock_path(project_id)
        lock_file.touch(exist_ok=True)
        with open(lock_file, "r") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

        return ProjectState.model_validate(data)

    def save_project(self, state: ProjectState) -> None:
        state.updated_at = datetime.now()
        self._save_state(state)

    def list_projects(self) -> list[ProjectState]:
        projects = []
        for d in sorted(self.projects_dir.iterdir()):
            state_file = d / "state.json"
            if state_file.exists():
                try:
                    projects.append(self.load_project(d.name))
                except Exception:
                    continue
        return projects

    def delete_project(self, project_id: str) -> None:
        project_dir = self.projects_dir / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def artifact_dir(self, project_id: str, stage: Stage) -> Path:
        return self.projects_dir / project_id / "artifacts" / stage.value

    def save_artifact_file(self, project_id: str, stage: Stage,
                           filename: str, content: str) -> Path:
        """Write artifact content to disk and return the path."""
        artifact_path = self.artifact_dir(project_id, stage) / filename
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")
        return artifact_path

    def read_artifact_file(self, project_id: str, artifact: Artifact) -> str:
        """Read artifact content from disk."""
        path = self.projects_dir / project_id / artifact.path
        return path.read_text(encoding="utf-8")

    def get_latest_artifacts(self, state: ProjectState,
                             types: Optional[list[ArtifactType]] = None) -> dict[ArtifactType, str]:
        """Read latest version of each artifact type. Returns type -> content mapping."""
        result = {}
        for atype in (types or list(ArtifactType)):
            artifact = state.latest_artifact(atype)
            if artifact:
                try:
                    content = self.read_artifact_file(state.project_id, artifact)
                    result[atype] = content
                except FileNotFoundError:
                    continue
        return result

    # --- internal ---

    def _lock_path(self, project_id: str) -> Path:
        """Path for the per-project lock file."""
        return self.projects_dir / project_id / "state.json.lock"

    def _save_state(self, state: ProjectState) -> None:
        """Atomic write with exclusive file lock.

        Sequence: acquire lock → write tmp → rename tmp → release lock.
        The lock file is separate from state.json so that tmp+rename
        atomicity is preserved (renaming the locked file itself would
        release the lock on some systems).
        """
        project_dir = self.projects_dir / state.project_id
        state_file = project_dir / "state.json"
        tmp_file = project_dir / "state.json.tmp"
        lock_file = self._lock_path(state.project_id)

        lock_file.touch(exist_ok=True)
        with open(lock_file, "r") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                tmp_file.write_text(
                    state.model_dump_json(indent=2),
                    encoding="utf-8",
                )
                tmp_file.replace(state_file)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def _slugify(text: str) -> str:
    """Simple slug: lowercase, replace spaces/special chars with hyphens."""
    import re
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:40]
