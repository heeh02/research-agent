"""GitHub integration — branch management, PR creation, and CI triggers.

This module handles the GitHub side of the pipeline:
- Creating experiment branches
- Committing artifacts and code
- Creating PRs for review
- Triggering CI/CD workflows
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional


class GitHubIntegration:
    """Manages Git/GitHub operations for the research pipeline."""

    def __init__(self, config: dict[str, Any], project_dir: Path):
        self.config = config
        self.project_dir = project_dir
        self.enabled = config.get("enabled", False)
        self.repo = config.get("repo")
        self.branch_prefix = config.get("branch_prefix", "exp/")

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            check=check,
        )

    def init_repo(self) -> bool:
        """Initialize git repo if not already initialized."""
        if not self.enabled:
            return False
        result = self._run_git("rev-parse", "--is-inside-work-tree", check=False)
        if result.returncode != 0:
            self._run_git("init")
            self._run_git("add", ".")
            self._run_git("commit", "-m", "Initial commit: research project setup")
            return True
        return False

    def create_experiment_branch(self, experiment_name: str) -> str:
        """Create a new branch for an experiment."""
        if not self.enabled:
            return ""
        branch_name = f"{self.branch_prefix}{experiment_name}"
        self._run_git("checkout", "-b", branch_name)
        return branch_name

    def commit_artifacts(self, message: str, paths: Optional[list[str]] = None) -> str:
        """Commit artifacts to the current branch."""
        if not self.enabled:
            return ""
        if paths:
            for p in paths:
                self._run_git("add", p)
        else:
            self._run_git("add", "artifacts/", "state.json")
        result = self._run_git("commit", "-m", message, check=False)
        if result.returncode == 0:
            # Get commit hash
            hash_result = self._run_git("rev-parse", "HEAD")
            return hash_result.stdout.strip()
        return ""

    def create_pr(self, title: str, body: str, base: str = "main") -> Optional[str]:
        """Create a GitHub PR using gh CLI. Returns PR URL."""
        if not self.enabled or not self.repo:
            return None
        result = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--base", base],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def get_current_branch(self) -> str:
        result = self._run_git("branch", "--show-current", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def push(self, branch: Optional[str] = None) -> bool:
        if not self.enabled:
            return False
        args = ["push", "-u", "origin"]
        if branch:
            args.append(branch)
        result = self._run_git(*args, check=False)
        return result.returncode == 0
