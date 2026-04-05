"""Core data models for the research agent system.

Defines the type system for stages, agents, artifacts, gates, and project state.
The state machine transitions are defined here as the source of truth.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Stage(str, Enum):
    """Research pipeline stages — ordered but transitions can go backward."""
    PROBLEM_DEFINITION = "problem_definition"
    LITERATURE_REVIEW = "literature_review"
    HYPOTHESIS_FORMATION = "hypothesis_formation"
    EXPERIMENT_DESIGN = "experiment_design"
    IMPLEMENTATION = "implementation"
    EXPERIMENTATION = "experimentation"
    ANALYSIS = "analysis"


# Ordered list for index-based operations
STAGE_ORDER: list[Stage] = list(Stage)


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    RESEARCHER = "researcher"
    CRITIC = "critic"
    ENGINEER = "engineer"


class CLIBackend(str, Enum):
    """Which CLI tool dispatches this agent."""
    CLAUDE = "claude"        # Claude Code CLI (claude -p)
    CODEX = "codex"          # OpenAI Codex CLI (codex exec)
    OPENCODE = "opencode"    # OpenCode CLI (opencode run) — supports Doubao/DeepSeek/etc.


# Keep LLMProvider as alias for backward compat with state.json serialization
class LLMProvider(str, Enum):
    CLAUDE = "claude"
    OPENAI = "openai"
    CODEX = "codex"
    OPENCODE = "opencode"
    LOCAL = "local"


class GateStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review"
    SKIPPED = "skipped"


class AutomationLevel(str, Enum):
    MANUAL = "manual"       # Human confirms every step
    HYBRID = "hybrid"       # Auto within stages, human at key gates
    FULL = "full"           # Fully automated, human only at configured gates


class ArtifactType(str, Enum):
    PROBLEM_BRIEF = "problem_brief"
    LITERATURE_MAP = "literature_map"
    EVIDENCE_TABLE = "evidence_table"
    HYPOTHESIS_CARD = "hypothesis_card"
    EXPERIMENT_SPEC = "experiment_spec"
    RUN_MANIFEST = "run_manifest"
    CODE = "code"
    TEST_RESULT = "test_result"
    EXPERIMENT_LOG = "experiment_log"
    METRICS = "metrics"
    RESULT_REPORT = "result_report"
    CLAIM_CHECKLIST = "claim_checklist"
    REVIEW_REPORT = "review_report"


# ---------------------------------------------------------------------------
# State machine: allowed transitions
# ---------------------------------------------------------------------------

# (from_stage, to_stage) -> trigger description
ALLOWED_TRANSITIONS: dict[tuple[Stage, Stage], str] = {
    # Forward transitions (gate must pass)
    (Stage.PROBLEM_DEFINITION, Stage.LITERATURE_REVIEW): "problem_defined",
    (Stage.LITERATURE_REVIEW, Stage.HYPOTHESIS_FORMATION): "literature_sufficient",
    (Stage.HYPOTHESIS_FORMATION, Stage.EXPERIMENT_DESIGN): "hypothesis_viable",
    (Stage.EXPERIMENT_DESIGN, Stage.IMPLEMENTATION): "spec_complete",
    (Stage.IMPLEMENTATION, Stage.EXPERIMENTATION): "code_ready",
    (Stage.EXPERIMENTATION, Stage.ANALYSIS): "results_available",
    # Backward transitions (rollbacks)
    (Stage.HYPOTHESIS_FORMATION, Stage.LITERATURE_REVIEW): "need_more_evidence",
    (Stage.EXPERIMENT_DESIGN, Stage.HYPOTHESIS_FORMATION): "hypothesis_needs_revision",
    (Stage.IMPLEMENTATION, Stage.EXPERIMENT_DESIGN): "design_flaw_found",
    (Stage.EXPERIMENTATION, Stage.IMPLEMENTATION): "code_bug_found",
    (Stage.ANALYSIS, Stage.EXPERIMENTATION): "need_more_experiments",
    (Stage.ANALYSIS, Stage.HYPOTHESIS_FORMATION): "hypothesis_falsified",
}


# Which agent is primary at each stage
STAGE_PRIMARY_AGENT: dict[Stage, AgentRole] = {
    Stage.PROBLEM_DEFINITION: AgentRole.RESEARCHER,
    Stage.LITERATURE_REVIEW: AgentRole.RESEARCHER,
    Stage.HYPOTHESIS_FORMATION: AgentRole.RESEARCHER,
    Stage.EXPERIMENT_DESIGN: AgentRole.ENGINEER,
    Stage.IMPLEMENTATION: AgentRole.ENGINEER,
    Stage.EXPERIMENTATION: AgentRole.ENGINEER,
    Stage.ANALYSIS: AgentRole.RESEARCHER,
}

# Which agent reviews at each gate
STAGE_REVIEWER: dict[Stage, AgentRole] = {
    Stage.PROBLEM_DEFINITION: AgentRole.CRITIC,
    Stage.LITERATURE_REVIEW: AgentRole.CRITIC,
    Stage.HYPOTHESIS_FORMATION: AgentRole.CRITIC,
    Stage.EXPERIMENT_DESIGN: AgentRole.CRITIC,
    Stage.IMPLEMENTATION: AgentRole.CRITIC,
    Stage.EXPERIMENTATION: AgentRole.ENGINEER,
    Stage.ANALYSIS: AgentRole.CRITIC,
}

# Required artifact types for each stage's gate to pass
STAGE_REQUIRED_ARTIFACTS: dict[Stage, list[ArtifactType]] = {
    Stage.PROBLEM_DEFINITION: [ArtifactType.PROBLEM_BRIEF],
    Stage.LITERATURE_REVIEW: [ArtifactType.LITERATURE_MAP, ArtifactType.EVIDENCE_TABLE],
    Stage.HYPOTHESIS_FORMATION: [ArtifactType.HYPOTHESIS_CARD],
    Stage.EXPERIMENT_DESIGN: [ArtifactType.EXPERIMENT_SPEC],
    Stage.IMPLEMENTATION: [ArtifactType.CODE, ArtifactType.TEST_RESULT],
    Stage.EXPERIMENTATION: [ArtifactType.RUN_MANIFEST, ArtifactType.METRICS],
    Stage.ANALYSIS: [ArtifactType.RESULT_REPORT, ArtifactType.CLAIM_CHECKLIST],
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class GateCheck(BaseModel):
    """A single check within a gate evaluation."""
    name: str
    description: str
    check_type: str  # "schema", "ai_eval", "automated", "human"
    passed: bool
    score: Optional[float] = None  # 0.0 - 1.0 for AI evals
    feedback: str = ""


class GateResult(BaseModel):
    """Result of a complete gate evaluation for a stage."""
    gate_name: str
    stage: Stage
    status: GateStatus
    checks: list[GateCheck] = []
    reviewer: Optional[AgentRole] = None
    overall_feedback: str = ""
    iteration: int = 1  # Which attempt at this gate
    timestamp: datetime = Field(default_factory=datetime.now)

    @property
    def pass_rate(self) -> float:
        if not self.checks:
            return 0.0
        return sum(1 for c in self.checks if c.passed) / len(self.checks)


class Artifact(BaseModel):
    """A structured deliverable produced at a stage."""
    name: str
    artifact_type: ArtifactType
    stage: Stage
    version: int = 1
    path: str  # Relative to project directory
    created_by: AgentRole
    created_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = {}


class StageTransition(BaseModel):
    """Record of a stage transition in project history."""
    from_stage: Optional[Stage]
    to_stage: Stage
    trigger: str
    gate_result: Optional[GateResult] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    notes: str = ""


class CostRecord(BaseModel):
    """Tracks API cost for a single LLM call."""
    agent: AgentRole
    provider: LLMProvider
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    task_description: str
    stage: Stage
    timestamp: datetime = Field(default_factory=datetime.now)


class AgentMessage(BaseModel):
    """A message exchanged between agents or with the orchestrator."""
    sender: AgentRole
    receiver: AgentRole
    content: str
    artifacts_referenced: list[str] = []
    stage: Stage
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Version timeline — tracks every agent interaction with semantic versioning
# Major = stage index (0..6), Minor = iteration within stage
# ---------------------------------------------------------------------------

class VersionEventType(str, Enum):
    AGENT_RUN = "agent_run"           # An agent produced output
    GATE_REVIEW = "gate_review"       # Critic reviewed artifacts
    GATE_PASSED = "gate_passed"       # Gate passed
    GATE_FAILED = "gate_failed"       # Gate failed
    STAGE_ADVANCE = "stage_advance"   # Major version bump
    STAGE_ROLLBACK = "stage_rollback" # Went back to earlier stage
    HUMAN_APPROVE = "human_approve"   # Human approved gate
    HUMAN_REJECT = "human_reject"     # Human rejected with feedback
    HUMAN_FEEDBACK = "human_feedback" # Human provided guidance


class VersionEvent(BaseModel):
    """A single event in the version timeline."""
    version: str                      # e.g. "2.3" = stage 2, iteration 3
    event_type: VersionEventType
    agent: Optional[AgentRole] = None
    stage: Stage
    summary: str                      # Short description of what happened
    detail: str = ""                  # Full output / feedback (truncated for display)
    artifacts_produced: list[str] = []
    artifacts_reviewed: list[str] = []
    gate_verdict: str = ""            # PASS / FAIL / REVISE
    scores: dict[str, float] = {}     # Gate criterion scores
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)


class ProjectState(BaseModel):
    """Complete state of a research project — serialized to disk."""
    project_id: str
    name: str
    description: str = ""
    research_question: str = ""
    current_stage: Stage = Stage.PROBLEM_DEFINITION
    artifacts: list[Artifact] = []
    gate_results: list[GateResult] = []
    transitions: list[StageTransition] = []
    cost_records: list[CostRecord] = []
    messages: list[AgentMessage] = []
    timeline: list[VersionEvent] = []  # Version timeline for GUI
    iteration_count: dict[str, int] = {}  # stage -> iteration count
    config_overrides: dict[str, Any] = {}
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # --- Version helpers ---

    def current_version(self) -> str:
        """Semantic version: major.minor (stage_index.iteration)."""
        major = STAGE_ORDER.index(self.current_stage)
        minor = self.iteration_count.get(self.current_stage.value, 1)
        return f"{major}.{minor}"

    def record_event(
        self,
        event_type: VersionEventType,
        summary: str,
        agent: Optional[AgentRole] = None,
        detail: str = "",
        artifacts_produced: list[str] | None = None,
        artifacts_reviewed: list[str] | None = None,
        gate_verdict: str = "",
        scores: dict[str, float] | None = None,
        cost_usd: float = 0.0,
        duration_seconds: float = 0.0,
    ):
        """Append an event to the version timeline."""
        self.timeline.append(VersionEvent(
            version=self.current_version(),
            event_type=event_type,
            agent=agent,
            stage=self.current_stage,
            summary=summary,
            detail=detail,
            artifacts_produced=artifacts_produced or [],
            artifacts_reviewed=artifacts_reviewed or [],
            gate_verdict=gate_verdict,
            scores=scores or {},
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
        ))
        self.updated_at = datetime.now()

    # --- Existing helpers ---

    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.cost_records)

    def stage_cost(self, stage: Stage) -> float:
        return sum(r.cost_usd for r in self.cost_records if r.stage == stage)

    def latest_artifact(self, artifact_type: ArtifactType) -> Optional[Artifact]:
        matches = [a for a in self.artifacts if a.artifact_type == artifact_type]
        return max(matches, key=lambda a: a.version, default=None)

    def stage_artifacts(self, stage: Stage) -> list[Artifact]:
        return [a for a in self.artifacts if a.stage == stage]

    def current_iteration(self) -> int:
        return self.iteration_count.get(self.current_stage.value, 1)

    def increment_iteration(self, stage: Optional[Stage] = None):
        """Increment the iteration count for a stage (used by revision loops)."""
        key = (stage or self.current_stage).value
        self.iteration_count[key] = self.iteration_count.get(key, 1) + 1
        self.updated_at = datetime.now()

    def record_transition(self, to_stage: Stage, trigger: str,
                          gate_result: Optional[GateResult] = None, notes: str = ""):
        transition = StageTransition(
            from_stage=self.current_stage,
            to_stage=to_stage,
            trigger=trigger,
            gate_result=gate_result,
            notes=notes,
        )
        self.transitions.append(transition)

        # Record timeline event
        from_idx = STAGE_ORDER.index(self.current_stage) if self.current_stage else -1
        to_idx = STAGE_ORDER.index(to_stage)
        if to_idx > from_idx:
            self.record_event(
                VersionEventType.STAGE_ADVANCE,
                f"Advanced: {self.current_stage.value} → {to_stage.value}",
                detail=notes,
            )
        else:
            self.record_event(
                VersionEventType.STAGE_ROLLBACK,
                f"Rolled back: {self.current_stage.value} → {to_stage.value}",
                detail=notes,
            )

        self.current_stage = to_stage
        stage_idx_from = STAGE_ORDER.index(transition.from_stage) if transition.from_stage else -1
        stage_idx_to = STAGE_ORDER.index(to_stage)
        if stage_idx_to <= stage_idx_from:
            key = to_stage.value
            self.iteration_count[key] = self.iteration_count.get(key, 1) + 1
        self.updated_at = datetime.now()
