"""Experiment tracking integration — MLflow and W&B support.

This module bridges the research agent pipeline with experiment tracking systems.
Every formal experiment run should be logged here, not just in chat.
"""

from __future__ import annotations

from typing import Any, Optional


class ExperimentTracker:
    """Unified interface for experiment tracking backends."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.enabled = config.get("enabled", False)
        self.backend = config.get("backend", "mlflow")
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return

        if not self.enabled:
            return

        if self.backend == "mlflow":
            import mlflow
            mlflow_cfg = self.config.get("mlflow", {})
            tracking_uri = mlflow_cfg.get("tracking_uri", "http://localhost:5000")
            mlflow.set_tracking_uri(tracking_uri)
            self._client = mlflow

        elif self.backend == "wandb":
            import wandb
            wandb_cfg = self.config.get("wandb", {})
            self._client = wandb

    def start_run(
        self,
        experiment_name: str,
        run_name: str,
        tags: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        """Start a tracked experiment run. Returns run ID."""
        if not self.enabled:
            return None

        self._ensure_client()

        if self.backend == "mlflow":
            import mlflow
            mlflow_cfg = self.config.get("mlflow", {})
            prefix = mlflow_cfg.get("experiment_prefix", "research-agent")
            exp_name = f"{prefix}/{experiment_name}"
            mlflow.set_experiment(exp_name)
            run = mlflow.start_run(run_name=run_name, tags=tags)
            return run.info.run_id

        elif self.backend == "wandb":
            import wandb
            wandb_cfg = self.config.get("wandb", {})
            run = wandb.init(
                project=wandb_cfg.get("project", "research-agent"),
                entity=wandb_cfg.get("entity"),
                name=run_name,
                tags=list((tags or {}).values()),
            )
            return run.id

        return None

    def log_params(self, params: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._ensure_client()

        if self.backend == "mlflow":
            import mlflow
            mlflow.log_params(params)
        elif self.backend == "wandb":
            import wandb
            wandb.config.update(params)

    def log_metrics(self, metrics: dict[str, float], step: Optional[int] = None) -> None:
        if not self.enabled:
            return
        self._ensure_client()

        if self.backend == "mlflow":
            import mlflow
            mlflow.log_metrics(metrics, step=step)
        elif self.backend == "wandb":
            import wandb
            wandb.log(metrics, step=step)

    def log_artifact(self, file_path: str, artifact_type: str = "") -> None:
        if not self.enabled:
            return
        self._ensure_client()

        if self.backend == "mlflow":
            import mlflow
            mlflow.log_artifact(file_path)
        elif self.backend == "wandb":
            import wandb
            artifact = wandb.Artifact(name=artifact_type or "artifact", type="output")
            artifact.add_file(file_path)
            wandb.log_artifact(artifact)

    def end_run(self, status: str = "FINISHED") -> None:
        if not self.enabled:
            return
        self._ensure_client()

        if self.backend == "mlflow":
            import mlflow
            mlflow.end_run(status=status)
        elif self.backend == "wandb":
            import wandb
            wandb.finish()
