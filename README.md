# Research Agent

**Multi-agent automated research system | 多智能体自动化科研系统**

[English](#english) | [中文](#中文)

---

<a id="english"></a>

## English

A **multi-agent automated research system** that orchestrates isolated AI agents through a structured 7-stage scientific pipeline — from problem definition to result analysis — with built-in quality gates, adversarial review, and human oversight.

Each agent's **CLI backend and model are fully configurable** — swap between Claude, Codex, OpenCode, or any supported model at any time, per agent, without changing code.

```
                    Problem          Literature        Hypothesis        Experiment        Implementation   Experimentation     Analysis
                   Definition         Review           Formation           Design                                               
                  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
                  │Researcher│───▶│Researcher│───▶│Researcher│───▶│ Engineer │───▶│ Engineer │───▶│ Engineer │───▶│Researcher│
                  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘
                       │               │               │               │               │               │               │
                  ┌────▼─────┐    ┌────▼─────┐    ┌────▼─────┐    ┌────▼─────┐    ┌────▼─────┐    ┌────▼─────┐    ┌────▼─────┐
                  │  Critic  │    │  Critic  │    │  Critic  │    │  Critic  │    │  Critic  │    │  Critic  │    │  Critic  │
                  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘
                       │               │               │               │               │               │               │
                    [Gate]          [Gate]        [Gate+Human]       [Gate]          [Gate]       [Gate+Human]       [Gate]
```

### Table of Contents

- [Why This Exists](#why-this-exists)
- [Core Principles](#core-principles)
- [Architecture](#architecture)
- [Pluggable Backend System](#pluggable-backend-system)
- [The Pipeline](#the-pipeline)
- [Agent Roles](#agent-roles)
- [Quality Gate System](#quality-gate-system)
- [Artifact Communication](#artifact-communication)
- [Storage Architecture](#storage-architecture)
- [Web GUI](#web-gui)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Directory Structure](#directory-structure)
- [Testing](#testing)

### Why This Exists

Running rigorous research is hard. Researchers skip literature review, engineers cut corners on reproducibility, and nobody wants to be their own critic. This system enforces the scientific method by separating concerns into isolated agents that **cannot** bypass each other:

- The **Researcher** cannot run code (preventing "just try it" shortcuts)
- The **Engineer** cannot change the hypothesis (preventing goalpost-moving)
- The **Critic** cannot rewrite artifacts (preventing conflicts of interest)
- The **Orchestrator** only manages state (preventing unauthorized LLM calls)

Every artifact is reviewed. Every transition is gated. Every result is independently verified.

### Core Principles

| Principle | Implementation |
|-----------|---------------|
| **Separation of Concerns** | Each agent has a strict toolset — the Researcher can't run code, the Engineer can't change hypotheses, the Critic can't write artifacts |
| **Pluggable Backends** | Every agent's CLI backend (Claude / Codex / OpenCode) and model are independently configurable — swap at any time via YAML or the Web GUI |
| **Artifact-Based Communication** | Agents never talk directly. All state flows through versioned YAML artifacts on disk |
| **Adversarial Review** | Every stage is reviewed by a Critic agent that is incentivized to find flaws, not approve work |
| **Independent Verification** | The Orchestrator materializes code and runs tests independently — agents cannot fabricate results |
| **Conservative Defaults** | Verdict parser defaults to REVISE (never PASS). Missing scores count as 0.0. Pre-checks can override the critic |
| **Human-in-the-Loop** | Configurable human gates at critical decision points (hypothesis, experimentation) |
| **Atomic Persistence** | State writes use tmp+rename to prevent corruption on crash |

### Architecture

```
Python Orchestrator (scripts/multi_agent.py)
│
├── [Any CLI Backend] ── Researcher Agent ── agents/researcher/CLAUDE.md
│   Tools: Read, Write, Glob, Grep, WebSearch, WebFetch, Agent
│   Default: Claude Opus 4  (configurable to any backend + model)
│
├── [Any CLI Backend] ── Engineer Agent ──── agents/engineer/CLAUDE.md
│   Tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Agent
│   Default: Doubao Seed 2.0 Code  (configurable to any backend + model)
│
├── [Any CLI Backend] ── Critic Agent ────── agents/critic/CLAUDE.md
│   Access: Full project sandbox (reads everything)
│   Default: GPT-5.4 via Codex  (configurable to any backend + model)
│
└── Human Gate ────────── Manual Approval
    Stages: hypothesis_formation, experimentation
```

### Pluggable Backend System

**Every agent can use any CLI backend and any model.** The backend determines how the agent process is launched; the model determines which LLM runs inside it. This can be changed at any time — via `config/settings.yaml`, via the Web GUI, or even between pipeline steps.

| Backend | Command | Supported Models | Best For |
|---------|---------|-------------------|----------|
| **Claude** | `claude -p` | Claude Opus 4, Sonnet 4, Haiku 4.5 | Deep reasoning, web research |
| **Codex** | `codex exec` | GPT-5.4, GPT-4.1, o4-mini | Full sandbox review, adversarial critique |
| **OpenCode** | `opencode run` | Doubao Seed 2.0, DeepSeek R1, Kimi, etc. | Code generation, cost-effective engineering |

**Example configurations:**

```yaml
# Scenario 1: All Claude (highest quality, highest cost)
agents:
  researcher: { backend: claude, model: claude-opus-4-20250514 }
  engineer:   { backend: claude, model: claude-opus-4-20250514 }
  critic:     { backend: claude, model: claude-opus-4-20250514 }

# Scenario 2: Mixed (balanced cost/quality)
agents:
  researcher: { backend: claude, model: claude-opus-4-20250514 }
  engineer:   { backend: opencode, model: volcengine-plan/doubao-seed-2.0-code }
  critic:     { backend: codex, model: gpt-5.4 }

# Scenario 3: Cost-optimized (use cheaper models)
agents:
  researcher: { backend: claude, model: claude-sonnet-4-20250514 }
  engineer:   { backend: opencode, model: deepseek/deepseek-r1 }
  critic:     { backend: codex, model: o4-mini }
```

### The Pipeline

#### 7 Stages

The pipeline implements a finite state machine with 7 ordered stages. Each stage has a primary agent, a reviewer, required artifacts, and weighted gate criteria.

| # | Stage | Primary Agent | Reviewer | Required Artifacts | Human Gate |
|---|-------|--------------|----------|-------------------|------------|
| 0 | **Problem Definition** | Researcher | Critic | `problem_brief` | No |
| 1 | **Literature Review** | Researcher | Critic | `literature_map`, `evidence_table` | No |
| 2 | **Hypothesis Formation** | Researcher | Critic | `hypothesis_card` | **Yes** |
| 3 | **Experiment Design** | Engineer | Critic | `experiment_spec` | No |
| 4 | **Implementation** | Engineer | Critic | `code`, `test_result` | No |
| 5 | **Experimentation** | Engineer | Engineer | `run_manifest`, `metrics` | **Yes** |
| 6 | **Analysis** | Researcher | Critic | `result_report`, `claim_checklist` | No |

#### State Transitions

Forward transitions require the current stage's gate to pass. Backward transitions (rollbacks) are triggered by specific failure types identified by the Critic.

```
Forward (gate must pass):
  problem_definition ──▶ literature_review ──▶ hypothesis_formation ──▶ experiment_design
       ──▶ implementation ──▶ experimentation ──▶ analysis

Backward (rollbacks):
  hypothesis_formation ◀── literature_review       (need_more_evidence)
  experiment_design    ◀── hypothesis_formation     (hypothesis_needs_revision)
  implementation       ◀── experiment_design        (design_flaw_found)
  experimentation      ◀── implementation           (code_bug_found)
  analysis             ◀── experimentation          (need_more_experiments)
  analysis             ──▶ hypothesis_formation     (hypothesis_falsified)
```

#### Revision Cycles

When a gate fails, the same agent is re-dispatched with the Critic's feedback. This continues up to `max_iterations` (default: 5) before escalating to human intervention.

```
 Agent produces artifact
        │
        ▼
 Critic reviews ──── PASS ───▶ Advance to next stage
        │
      REVISE
        │
        ▼
 Same agent re-dispatched with feedback
        │
        ▼
 (repeat up to max_iterations)
        │
      FAIL / exhausted
        │
        ▼
 Cross-stage rollback OR human escalation
```

#### Semantic Versioning

Every event is tracked with semantic versioning: `major.minor` where `major` = stage index (0-6) and `minor` = iteration count within that stage. This provides a complete audit trail.

### Agent Roles

#### Researcher

The Researcher handles all knowledge work: literature analysis, gap identification, hypothesis formation, and result interpretation.

**Capabilities**: Web search, academic paper retrieval, structured analysis, subagent spawning  
**Constraints**: Cannot run code, cannot design experiments  
**Citation Protocol**: Every paper must have a verifiable URL. Papers the agent is less than 90% confident exist must be marked `verified: false`. It is better to cite 5 real papers than 15 with 10 fabricated.

**Produced Artifacts**:
- `problem_brief` — Research problem, scope, motivation, existing approaches
- `literature_map` + `evidence_table` — Surveyed papers, key findings, identified gaps
- `hypothesis_card` — Falsifiable claim, kill criteria, testable predictions
- `result_report` + `claim_checklist` — Analysis with evidence-backed claims

#### Engineer

The Engineer translates hypotheses into executable experiments.

**Capabilities**: Code writing, testing, debugging, bash execution, subagent spawning  
**Constraints**: Cannot change the hypothesis, cannot make quality judgments  
**Code Quality**: Must use real datasets (DummyDataset is an automatic FAIL), pin all random seeds, include tests for critical paths.

**Draft vs. Actual Results**: The Engineer produces *draft* test results. The Orchestrator independently materializes code to disk, runs `pytest`, and produces *verified* results. The Critic reviews the verified output, not the draft.

**Produced Artifacts**:
- `experiment_spec` — Complete experimental design with datasets, models, metrics, ablations
- `code` + `test_result` — Implementation with passing tests
- `run_manifest` + `metrics` — Execution manifest and measured results

#### Critic

The Critic is an adversarial reviewer. It is skeptical by default and incentivized to find flaws.

**Capabilities**: Full project sandbox access (reads everything)  
**Constraints**: Cannot write files, cannot create artifacts, cannot run commands  
**Verdict**: Outputs structured YAML with `PASS` / `REVISE` / `FAIL`, per-criterion scores (0.0-1.0), blocking issues, and a classified `failure_type` that routes the pipeline's rollback logic.

**Stage-Specific Checks**:
- *Literature Review*: Flags papers without URLs; >50% suspected fabrication = automatic FAIL
- *Implementation*: Reviews actual test output from Orchestrator, not agent self-reports
- *Experimentation*: Metrics that "just barely" exceed all targets = HIGHLY suspicious
- *Analysis*: Claims without specific experimental evidence = automatic REVISE

#### Orchestrator

The Orchestrator is pure Python — no LLM calls. It manages state, dispatches agents, assembles context, materializes code, runs tests independently, and interprets gate results.

**Key Operations**:
1. Build task cards with context from prior artifacts and any previous feedback
2. Dispatch the appropriate agent via the configured CLI backend
3. Parse output and register produced artifacts
4. Dispatch the Critic for review
5. Interpret the verdict and route (advance / revise / rollback / escalate)
6. Track version timeline, costs, and transitions

### Quality Gate System

Every stage must pass a **three-layer quality gate** before the pipeline advances.

#### Layer 1: Schema Validation (Automated)

Each artifact type has a YAML schema (in `schemas/`) defining required fields, field types, and minimum list lengths. The gate verifies structural correctness before any AI review.

**14 schemas defined**: `problem_brief`, `literature_map`, `evidence_table`, `hypothesis_card`, `experiment_spec`, `code`, `test_result`, `run_manifest`, `metrics`, `result_report`, `claim_checklist`, `review_report`, `experiment_log`, `task_card`

#### Layer 2: Pre-Review Checks (Domain-Specific)

Structural checks that catch common failure modes before spending tokens on critic review:

| Stage | Check | Action |
|-------|-------|--------|
| Literature Review | Verify paper URLs are reachable | Flag hallucinated citations |
| Implementation | Detect `DummyDataset` usage | Automatic FAIL |
| Implementation | Run pytest and capture results | Override draft with actual output |
| Experimentation | Validate metrics are real numbers | Flag fabricated results |
| Analysis | Check claim checklist completion | REVISE if < 50% complete |

#### Layer 3: Critic Review (AI Evaluation)

The Critic scores each stage against weighted criteria (all summing to 1.0). The weighted average must meet `pass_threshold` (default: 0.7) with no blocking issues.

**Example — Problem Definition Gate**:
| Criterion | Weight | Description |
|-----------|--------|-------------|
| Clarity | 0.20 | Problem statement is unambiguous |
| Significance | 0.20 | Addresses a real and important gap |
| Scope | 0.20 | Neither too broad nor too narrow |
| Novelty | 0.20 | Genuinely different from existing work |
| Feasibility | 0.20 | Can be investigated with available resources |

#### Failure Type Routing

When the Critic identifies a failure, the `failure_type` field determines the pipeline's response:

| failure_type | Meaning | Pipeline Action |
|---|---|---|
| `structural_issue` | Schema violations, missing fields | Same-stage revision |
| `implementation_bug` | Code crashes, tests fail | Same-stage revision at Implementation |
| `design_flaw` | Experiment spec is incomplete or wrong | Rollback to Experiment Design |
| `hypothesis_needs_revision` | Hypothesis is untestable or vague | Rollback to Hypothesis Formation |
| `evidence_insufficient` | Not enough data points or experiments | Rollback to Experimentation |
| `hypothesis_falsified` | Results disprove the hypothesis | Rollback to Hypothesis Formation |
| `analysis_gap` | Claims not grounded in evidence | Same-stage revision at Analysis |

### Artifact Communication

Agents communicate **exclusively** through versioned YAML artifacts. There is no shared memory, no message passing, and no conversation between agents.

#### Lifecycle

```
1. Orchestrator builds TaskCard with context from prior artifacts
2. Agent receives TaskCard + role instructions (CLAUDE.md)
3. Agent writes artifact YAML to: projects/<id>/artifacts/<stage>/<type>_v<N>.yaml
4. Orchestrator registers artifact in ProjectState
5. Critic receives artifact for review
6. On revision: agent receives Critic feedback in next TaskCard
```

#### Versioning

Artifacts follow the naming convention `<type>_v<version>.yaml`. Versions are monotonically increasing within a stage. All artifacts are immutable historical records — revisions create new versions, never overwrite.

#### Example Artifact (hypothesis_card)

```yaml
claim: "Selective attention pruning reduces VLA inference latency by 40%
        while maintaining 95% task success rate"
motivation: "Current VLAs require 32+ A100 GPUs for inference..."
why_now: "Recent work on attention head importance scoring enables..."
novelty_argument: "Unlike uniform pruning, we propose task-conditioned selection..."
key_assumptions:
  - "Attention heads have heterogeneous importance across modalities"
  - "Pruning patterns generalize across similar manipulation tasks"
testable_predictions:
  - "Pruned model achieves >95% success on SIMPLER benchmark"
  - "Inference latency decreases >40% on single A100"
kill_criteria:
  - "If success rate drops below 85%, the approach is not viable"
  - "If latency reduction is <20%, the overhead is not justified"
key_risks:
  - "Pruning may disproportionately affect rare manipulation types"
  - "Importance scores computed on one task family may not transfer"
```

### Storage Architecture

The system uses a **file-based, zero-database persistence model**. All state is stored as JSON and YAML on the local filesystem, with no external database dependency. This section details the five storage layers, their data models, write semantics, and how they interact.

#### Layer 1: ProjectState — The Central State Store

Every project has a single source of truth: `projects/<id>/state.json`. This file is a JSON-serialized [Pydantic](https://docs.pydantic.dev/) `ProjectState` model containing the complete project lifecycle.

**Data Model** (`src/research_agent/models.py`):

```
ProjectState
├── project_id: str                    # Slug + UUID suffix (e.g. "vla-model-0ddc306e")
├── name: str                          # Human-readable project name
├── description: str
├── research_question: str             # The driving research question
├── current_stage: Stage               # Enum: one of 7 stages
├── artifacts: list[Artifact]          # Every artifact ever produced (append-only)
├── gate_results: list[GateResult]     # Every gate evaluation ever run (append-only)
├── transitions: list[StageTransition] # Stage change history (append-only)
├── cost_records: list[CostRecord]     # Per-call API cost tracking (append-only)
├── messages: list[AgentMessage]       # Inter-agent messages (append-only)
├── timeline: list[VersionEvent]       # Semantic-versioned event log (append-only)
├── iteration_count: dict[str, int]    # stage_name → current iteration number
├── config_overrides: dict             # Per-project config overrides
├── created_at: datetime
└── updated_at: datetime
```

**Key sub-models stored inline in state.json:**

| Model | Fields | Purpose |
|-------|--------|---------|
| `Artifact` | name, artifact_type, stage, version, path, created_by, created_at, metadata, provenance | Registry of every artifact file, with its disk path and creation provenance |
| `GateResult` | gate_name, stage, status, checks[], reviewer, overall_feedback, iteration, timestamp | Full gate evaluation result, including per-criterion scores and feedback text |
| `GateCheck` | name, description, check_type, passed, score, feedback | Individual check within a gate (schema / codex / automated / human) |
| `StageTransition` | from_stage, to_stage, trigger, gate_result, timestamp, notes | Record of each forward advance or backward rollback |
| `CostRecord` | agent, provider, model, input_tokens, output_tokens, cost_usd, task_description, stage, timestamp | Per-LLM-call cost tracking for budget enforcement |
| `VersionEvent` | version, event_type, agent, stage, summary, detail, artifacts_produced, gate_verdict, scores, cost_usd, duration_seconds, timestamp | Semantic-versioned audit event for the timeline |

**Append-only invariant**: The `artifacts`, `gate_results`, `transitions`, `cost_records`, and `timeline` lists are strictly append-only. No entry is ever modified or deleted. This makes `state.json` a complete, auditable log of everything that happened in the project — you can reconstruct the exact sequence of agent runs, gate evaluations, rollbacks, and costs by reading it.

**Atomic write protocol** (`state.py:_save_state`):

```python
def _save_state(self, state: ProjectState) -> None:
    state_file = project_dir / "state.json"
    tmp_file   = project_dir / "state.json.tmp"
    # 1. Serialize to temporary file
    tmp_file.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    # 2. Atomic rename (POSIX guarantees this is atomic on same filesystem)
    tmp_file.replace(state_file)
```

If the process crashes during step 1, `state.json` is untouched. If it crashes during step 2, the rename is atomic — the file is either the old version or the new version, never a partial write. This eliminates corruption without WAL, journaling, or locking.

**Semantic versioning** (`ProjectState.current_version`):

```
Version = {stage_index}.{iteration_count}
Example: "2.3" = Stage 2 (hypothesis_formation), iteration 3
```

This is computed, not stored — derived from `current_stage` and `iteration_count`. Every `VersionEvent` in the timeline records this version, creating a human-readable audit trail.

#### Layer 2: Artifact Files — The Inter-Agent Communication Channel

Agents communicate **exclusively** through versioned YAML files on disk. Each artifact has a canonical path derived from its type and version number.

**Storage layout:**

```
projects/<project-id>/
└── artifacts/
    ├── problem_definition/
    │   └── problem_brief_v1.yaml          # Researcher output, iteration 1
    ├── literature_review/
    │   ├── literature_map_v1.yaml          # First attempt
    │   ├── literature_map_v2.yaml          # After critic feedback
    │   └── evidence_table_v1.yaml
    ├── hypothesis_formation/
    │   └── hypothesis_card_v1.yaml
    ├── experiment_design/
    │   └── experiment_spec_v1.yaml
    ├── implementation/
    │   ├── code_v1.yaml                    # Engineer's code (YAML-wrapped)
    │   ├── test_result_v1.yaml             # Engineer's draft
    │   └── test_result_v2.yaml             # Orchestrator's VERIFIED result
    ├── experimentation/
    │   ├── run_manifest_v1.yaml
    │   ├── metrics_v1.yaml                 # Engineer's draft
    │   └── metrics_v2.yaml                 # Orchestrator's VERIFIED metrics
    └── analysis/
        ├── result_report_v1.yaml
        └── claim_checklist_v1.yaml
```

**Canonical path derivation** (`artifacts.py:create_artifact`):

The path is **always** derived from `artifact_type + computed_version`, never from the filename the agent writes. This prevents a critical class of bugs where state.json records version N but the file on disk is version M:

```python
canonical = f"{artifact_type.value}_v{version}.yaml"
rel_path  = f"artifacts/{stage.value}/{canonical}"
```

If an agent writes to a non-canonical filename, `register_artifact_file` renames the file to match.

**Immutability**: Artifacts are never overwritten. A revision always creates `_v{N+1}`. The `state.latest_artifact(type)` method returns the highest-version artifact for a given type, but all previous versions remain on disk as historical records.

**Schema enforcement** (`schemas/*.schema.yaml`):

Each of the 14 artifact types has a YAML schema defining structural constraints:

```yaml
# schemas/hypothesis_card.schema.yaml
required_fields:
  - claim
  - motivation
  - key_assumptions
  - testable_predictions
  - kill_criteria

field_types:
  claim: string
  key_assumptions: list
  testable_predictions: list

min_lengths:
  key_assumptions: 2
  testable_predictions: 2
  kill_criteria: 2
```

The gate system validates every artifact against its schema **before** any AI review. Schema validation catches structural failures (missing fields, wrong types, insufficient list entries) at zero token cost.

#### Layer 3: Materialized Code — From YAML to Executable

The `code` artifact is special. It stores source files **inside a YAML wrapper**:

```yaml
# artifacts/implementation/code_v1.yaml
files:
  - path: experiments/train.py
    content: |
      import torch
      from datasets import load_dataset
      ...
  - path: experiments/model.py
    content: |
      class VLAModel(nn.Module):
      ...
  - path: experiments/test_model.py
    content: |
      def test_forward_pass():
      ...
```

The Orchestrator **materializes** this YAML to real files on disk (`execution.py:materialize_code`):

```
projects/<id>/experiments/train.py       ← extracted from code_v1.yaml
projects/<id>/experiments/model.py       ← extracted from code_v1.yaml
projects/<id>/experiments/test_model.py  ← extracted from code_v1.yaml
```

After materialization, the Orchestrator runs `pytest` and the smoke test from `run_manifest` independently. The results are written back as **verified** `test_result` and `metrics` artifacts (with `verified_by: orchestrator` metadata), overriding any draft artifacts the Engineer produced. This is the key anti-fabrication mechanism — the Critic reviews the Orchestrator's verified output, not the Engineer's self-report.

#### Layer 4: Execution Logs — Raw Process Output

```
projects/<id>/
└── logs/
    ├── problem_definition-a1b2c3.txt    # Raw stdout/stderr from agent dispatch
    ├── experiment_execution.txt          # Smoke test / experiment output
    └── ...
```

Log files capture the complete raw output of every agent subprocess and experiment execution. These are reference-only — they are not parsed by the pipeline (verdict parsing uses the structured YAML output), but they are invaluable for debugging failures.

#### Layer 5: Configuration — Pipeline Behavior

Configuration is split across two YAML files, both human-editable:

**`config/settings.yaml`** — Runtime configuration:
```
settings.yaml
├── agents:                    # Per-agent: backend, model, effort, max_turns, allowed_tools
│   ├── researcher: {...}
│   ├── engineer: {...}
│   ├── critic: {...}
│   └── orchestrator: {...}
├── pipeline:
│   ├── automation_level       # manual | hybrid | full
│   ├── human_gates: [...]     # Which stages need human approval
│   ├── max_iterations: 5      # Max revision cycles per stage
│   └── confirm_before_advance
├── cost:
│   ├── warning_threshold      # Alert at this USD amount
│   ├── hard_limit             # Stop pipeline at this USD amount
│   └── codex_estimated_cost_per_review
├── gui: { host, port }
├── github: { enabled }
└── tracking: { backend, enabled }
```

**`config/stages.yaml`** — Stage definitions (structural, rarely changed):
```
stages.yaml
├── stages:
│   └── <stage_name>:
│       ├── description         # Bilingual stage description
│       ├── primary_agent       # Who produces artifacts
│       ├── reviewer            # Who reviews them
│       ├── required_artifacts  # What must be produced
│       ├── gate_criteria:      # Weighted scoring rubric
│       │   └── [{name, description, weight}]
│       ├── pass_threshold      # Minimum weighted average (default: 0.7)
│       └── human_gate          # true/false
└── rollback_rules:
    └── [{from, to, trigger, description}]
```

The Web GUI can modify `settings.yaml` at runtime via the `/api/config` REST endpoint. Changes take effect on the next pipeline step without restarting.

#### Data Flow Summary

```
 ┌───────────────────────────────────────────────────────────────────────┐
 │                         config/settings.yaml                         │
 │                         config/stages.yaml                           │
 │                    (read at dispatch time, hot-reloadable)            │
 └──────────────────────────────┬────────────────────────────────────────┘
                                │
                                ▼
 ┌──────────────────── Python Orchestrator ──────────────────────────────┐
 │                                                                       │
 │  1. Load state.json                                                   │
 │  2. Read config → determine agent, backend, model                     │
 │  3. Assemble context from prior artifacts                             │
 │  4. Build TaskCard (YAML)                                             │
 │  5. Dispatch agent subprocess                                         │
 │     ├── Agent writes artifact YAML to artifacts/<stage>/              │
 │     └── Raw output saved to logs/                                     │
 │  6. Register artifact in state.artifacts[]                            │
 │  7. [Implementation] Materialize code → experiments/                  │
 │  8. [Implementation] Run pytest → write verified test_result          │
 │  9. Dispatch critic → review artifacts                                │
 │ 10. Parse verdict, scores, failure_type                               │
 │ 11. Append GateResult to state.gate_results[]                         │
 │ 12. Append VersionEvent to state.timeline[]                           │
 │ 13. Append StageTransition to state.transitions[]                     │
 │ 14. Atomic save state.json                                            │
 │ 15. Loop or advance                                                   │
 │                                                                       │
 └───────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────┼───────────────┐
                    ▼           ▼               ▼
              state.json   artifacts/       experiments/
             (central     (YAML files,     (materialized
              state)       immutable,       source code)
                           versioned)
```

#### Why File-Based, Not Database?

1. **Git-friendly**: Every state change is a file diff. `git log` gives you project history. `git diff state.json` shows exactly what changed.
2. **Zero infrastructure**: No PostgreSQL, no Redis, no Docker. `pip install` and you're running.
3. **Agent-compatible**: LLM agents can Read/Write files natively. No ORM, no SQL, no API client needed.
4. **Debuggable**: `cat state.json | jq '.timeline[-1]'` shows the latest event. `cat artifacts/implementation/code_v1.yaml` shows exactly what the Engineer wrote. No query language needed.
5. **Portable**: Copy the `projects/<id>/` directory and you have the complete project — state, artifacts, code, logs — in one place.
6. **Crash-safe**: Atomic tmp+rename writes prevent corruption without transactions.

### Web GUI

Launch the browser-based control panel:

```bash
python scripts/multi_agent.py gui --port 8080
# Visit http://localhost:8080
```

The GUI is a **full-featured single-page application** (dark theme, responsive layout) that provides complete pipeline control without touching the command line.

#### Project Management

- **Project sidebar**: Create, switch, and delete projects from a persistent left panel
- **Project creation modal**: Set project name and research question
- **Multi-project**: Work on multiple research projects, each with independent state

#### Pipeline Control

- **Auto / Step / Review** buttons with real-time status pill (Idle / Running / Waiting for Approval)
- **Stop** button to cancel running operations mid-execution
- **Until-stage selector**: Auto-run the pipeline up to a chosen stage
- **Max revisions**: Control how many revision cycles before escalation (1 / 2 / 3 / 5)
- **Custom instruction**: Inject per-step instructions to guide the current agent
- **Human gate controls**: Approve / Reject buttons appear when the pipeline reaches a human gate, with an optional feedback field

#### Agent Configuration Panel

- **Per-agent cards** for Researcher, Engineer, Critic, and Orchestrator
- **CLI Backend dropdown**: Switch between `claude`, `codex`, `opencode` per agent
- **Model dropdown**: Dynamically populated based on the selected backend
- **Effort dropdown**: `low` / `medium` / `high` / `max` / `xhigh` (varies by backend)
- **Live save**: Changes apply to the next pipeline step without restart

#### Version Timeline

- **Left panel**: Scrollable timeline grouped by semantic version (`major.minor`)
- **Stage filter dropdown**: Filter timeline events by pipeline stage
- **Event icons**: Color-coded by agent role (blue=Researcher, green=Engineer, purple=Critic, orange=Human)
- **Click to expand**: Select any version to see full event detail in the right panel

#### Event Detail Panel

- **Agent badge**: Shows which agent produced this event
- **Verdict display**: Color-coded PASS (green) / REVISE (yellow) / FAIL (red)
- **Score chips**: Per-criterion scores with pass/fail coloring
- **Artifact links**: Click to view full artifact content in a full-screen viewer modal
- **Detail text**: Expandable/collapsible agent output with monospace formatting
- **Expand All / Collapse All**: Toggle for bulk detail viewing

#### Analytics Dashboard

- **Per-stage cost bars**: Horizontal bar chart showing USD spent per pipeline stage
- **Per-stage duration bars**: Time spent per stage
- **Per-agent cost breakdown**: How much each agent role has consumed
- **Total cost and event count** displayed in the header

#### Console

- **Real-time log streaming**: Live output from agent processes with 2-second polling
- **Error highlighting**: Error lines rendered in red
- **Resizable**: Drag to resize the console panel vertically
- **Adaptive polling**: 2s when pipeline is running, 8s when idle

### Installation

#### Prerequisites

- Python >= 3.11
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude --version`)
- [OpenAI Codex CLI](https://github.com/openai/codex) (`npm install -g @openai/codex && codex login`)
- [OpenCode CLI](https://github.com/opencode-ai/opencode) (optional, for Doubao/DeepSeek backends)

#### Install

```bash
# Clone the repository
git clone https://github.com/heeh02/research-agent.git
cd research-agent

# Install in editable mode
pip install -e .

# Install dev dependencies (for testing)
pip install -e ".[dev]"

# Verify CLI tools
claude --version
codex --version
```

### Quick Start

#### 1. Create a Project

```bash
python scripts/pipeline.py init "VLA Model Efficiency" \
  -q "Can selective attention pruning reduce VLA inference cost by 40% without significant accuracy loss?"
```

This creates a project directory under `projects/` with an initialized `state.json`.

#### 2. Run the Pipeline

**Fully automated** (advances through all stages, pausing at human gates):
```bash
python scripts/multi_agent.py auto
```

**Step-by-step** (one stage at a time, with confirmation):
```bash
python scripts/multi_agent.py step
```

**Run until a specific stage**:
```bash
python scripts/multi_agent.py auto --until hypothesis_formation
```

**Web GUI** (full visual control):
```bash
python scripts/multi_agent.py gui --port 8080
```

#### 3. Check Status

```bash
python scripts/multi_agent.py status
```

#### 4. View Timeline

```bash
python scripts/multi_agent.py timeline
```

#### Operating Modes

| Mode | Command | Description |
|------|---------|-------------|
| Full Auto | `python scripts/multi_agent.py auto` | Runs all stages, pauses at human gates |
| Step-by-Step | `python scripts/multi_agent.py step` | One stage per invocation |
| Review Only | `python scripts/multi_agent.py review` | Run Critic on current artifacts |
| Web GUI | `python scripts/multi_agent.py gui` | Full visual control panel |
| Single-Agent | `python scripts/pipeline.py run` | All roles in one process (quick exploration) |

### Configuration

All configuration lives in `config/settings.yaml`. Settings can also be changed live via the Web GUI.

#### Agent Configuration

```yaml
agents:
  researcher:
    backend: claude                          # CLI backend: claude | codex | opencode
    model: claude-opus-4-20250514            # Model (backend-specific)
    effort: max                              # Effort: low | medium | high | max
    max_turns: 30                            # Max conversation turns
    allowed_tools: Read,Write,Glob,Grep,WebSearch,WebFetch,Agent

  engineer:
    backend: opencode
    model: volcengine-plan/doubao-seed-2.0-code
    effort: high
    max_turns: 40
    allowed_tools: Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent

  critic:
    backend: codex
    model: gpt-5.4
    effort: xhigh
```

#### Pipeline Configuration

```yaml
pipeline:
  automation_level: hybrid    # manual | hybrid | full
  human_gates:                # Stages requiring human approval
    - hypothesis_formation
    - experimentation
  max_iterations: 5           # Max revision cycles per stage
  confirm_before_advance: true
```

#### Cost Controls

```yaml
cost:
  warning_threshold: 5.0      # Warn at $5 spent
  hard_limit: 50.0             # Stop pipeline at $50
  codex_estimated_cost_per_review: 0.10
```

### Directory Structure

```
research-agent/
├── agents/
│   ├── researcher/CLAUDE.md     # Researcher role instructions & constraints
│   ├── engineer/CLAUDE.md       # Engineer role instructions & constraints
│   └── critic/CLAUDE.md         # Critic role instructions & verdict format
├── config/
│   ├── settings.yaml            # Agent backends, models, pipeline settings, cost limits
│   └── stages.yaml              # Stage definitions, gate criteria, rollback rules
├── schemas/                     # YAML schemas for all 14 artifact types
├── scripts/
│   ├── multi_agent.py           # Main orchestrator — auto, step, review, gui
│   ├── pipeline.py              # State management CLI — init, status, advance
│   └── setup.sh                 # One-click setup
├── src/research_agent/
│   ├── models.py                # Pydantic models, state machine, enums
│   ├── state.py                 # Atomic state persistence (JSON)
│   ├── artifacts.py             # Schema validation, artifact creation, context assembly
│   ├── dispatcher.py            # Multi-backend agent dispatch (claude/codex/opencode)
│   ├── gates.py                 # Three-layer gate evaluation
│   ├── verdict.py               # Verdict parsing, weighted scoring, rollback routing
│   ├── prechecks.py             # Domain-specific pre-review structural checks
│   ├── execution.py             # Code materialization, test execution
│   ├── gui.py                   # Web GUI (SPA with REST API)
│   └── integrations/
│       └── codex.py             # Codex CLI integration for critic reviews
├── tests/                       # 7 test modules covering all core logic
├── projects/                    # Project workspaces (one per research project)
│   └── <project-id>/
│       ├── state.json           # Complete project state
│       ├── artifacts/<stage>/   # Versioned YAML artifacts
│       ├── implementations/     # Materialized code
│       └── logs/                # Execution logs
├── pyproject.toml
└── CLAUDE.md                    # Top-level orchestrator instructions
```

### Testing

```bash
# Run the full test suite
pytest tests/ -v

# Run a specific test module
pytest tests/test_verdict.py -v

# Run with coverage
pytest tests/ --cov=src/research_agent
```

Key test invariants:
- `parse_verdict()` never defaults to PASS — ambiguous output always returns REVISE
- Missing gate scores count as 0.0 — agents cannot bypass gates by omission
- State machine transitions are validated against `ALLOWED_TRANSITIONS`
- Weighted scoring with missing fields is tested against known edge cases

---

<a id="中文"></a>

## 中文

一个**多智能体自动化科研系统**，通过结构化的 7 阶段科研流水线编排相互隔离的 AI 智能体——从问题定义到结果分析——内置质量门控、对抗性评审和人类监督。

每个智能体的 **CLI 后端和模型均可自由配置**——可随时在 Claude、Codex、OpenCode 或任何支持的模型之间切换，无需修改代码。

### 目录

- [设计动机](#设计动机)
- [核心原则](#核心原则)
- [系统架构](#系统架构)
- [可插拔后端系统](#可插拔后端系统)
- [研究流水线](#研究流水线)
- [智能体角色](#智能体角色)
- [质量门控系统](#质量门控系统)
- [制品通信机制](#制品通信机制)
- [存储架构](#存储架构)
- [Web 图形界面](#web-图形界面)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [目录结构](#目录结构)
- [测试](#测试)

### 设计动机

严谨的科研很难做到。研究者会跳过文献综述，工程师会在可复现性上偷工减料，没有人愿意做自己的批评者。本系统通过将职责分离到相互隔离的智能体中来强制执行科学方法——它们**无法**绕过彼此：

- **研究者**不能运行代码（防止「先试试再说」的捷径）
- **工程师**不能修改假设（防止移动终点线）
- **评审者**不能改写制品（防止利益冲突）
- **编排器**只管理状态（防止未授权的 LLM 调用）

每个制品都被评审。每次转换都经过门控。每个结果都被独立验证。

### 核心原则

| 原则 | 实现方式 |
|------|---------|
| **职责分离** | 每个智能体有严格的工具集——研究者不能跑代码，工程师不能改假设，评审者不能写制品 |
| **可插拔后端** | 每个智能体的 CLI 后端（Claude / Codex / OpenCode）和模型可独立配置——随时通过 YAML 或 Web GUI 切换 |
| **基于制品的通信** | 智能体之间不直接对话，所有状态通过磁盘上的版本化 YAML 制品流转 |
| **对抗性评审** | 每个阶段都由 Critic 智能体评审，其职责是发现缺陷而非批准工作 |
| **独立验证** | 编排器独立地将代码落盘并运行测试——智能体无法伪造结果 |
| **保守默认值** | 判定解析器默认返回 REVISE（永不默认 PASS），缺失分数计为 0.0，预检查可以覆盖 Critic 的判定 |
| **人在回路** | 可配置的人工门控，在关键决策点（假设形成、实验执行）暂停等待人工审批 |
| **原子持久化** | 状态写入使用 tmp+rename 模式，防止崩溃导致数据损坏 |

### 系统架构

```
Python 编排器 (scripts/multi_agent.py)
│
├── [任意 CLI 后端] ── 研究者智能体 ── agents/researcher/CLAUDE.md
│   工具: Read, Write, Glob, Grep, WebSearch, WebFetch, Agent
│   默认: Claude Opus 4（可配置为任意后端 + 模型）
│
├── [任意 CLI 后端] ── 工程师智能体 ── agents/engineer/CLAUDE.md
│   工具: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Agent
│   默认: Doubao Seed 2.0 Code（可配置为任意后端 + 模型）
│
├── [任意 CLI 后端] ── 评审者智能体 ── agents/critic/CLAUDE.md
│   权限: 完整项目沙箱访问（可读取所有内容）
│   默认: GPT-5.4 via Codex（可配置为任意后端 + 模型）
│
└── 人工门控 ────────── 手动审批
    阶段: hypothesis_formation, experimentation
```

### 可插拔后端系统

**每个智能体都可以使用任意 CLI 后端和任意模型。** 后端决定智能体进程的启动方式，模型决定内部运行的 LLM。这可以随时更改——通过 `config/settings.yaml`、通过 Web GUI，甚至在流水线步骤之间切换。

| 后端 | 命令 | 支持的模型 | 最适合 |
|------|------|-----------|--------|
| **Claude** | `claude -p` | Claude Opus 4, Sonnet 4, Haiku 4.5 | 深度推理、网络检索 |
| **Codex** | `codex exec` | GPT-5.4, GPT-4.1, o4-mini | 全沙箱评审、对抗性审查 |
| **OpenCode** | `opencode run` | Doubao Seed 2.0, DeepSeek R1, Kimi 等 | 代码生成、高性价比工程 |

**配置示例：**

```yaml
# 场景 1: 全部使用 Claude（最高质量，最高成本）
agents:
  researcher: { backend: claude, model: claude-opus-4-20250514 }
  engineer:   { backend: claude, model: claude-opus-4-20250514 }
  critic:     { backend: claude, model: claude-opus-4-20250514 }

# 场景 2: 混合搭配（平衡成本与质量）
agents:
  researcher: { backend: claude, model: claude-opus-4-20250514 }
  engineer:   { backend: opencode, model: volcengine-plan/doubao-seed-2.0-code }
  critic:     { backend: codex, model: gpt-5.4 }

# 场景 3: 成本优化（使用更便宜的模型）
agents:
  researcher: { backend: claude, model: claude-sonnet-4-20250514 }
  engineer:   { backend: opencode, model: deepseek/deepseek-r1 }
  critic:     { backend: codex, model: o4-mini }
```

### 研究流水线

#### 7 个阶段

流水线实现了一个有限状态机，包含 7 个有序阶段。每个阶段有一个主智能体、一个评审者、必需的制品和加权的门控标准。

| # | 阶段 | 主智能体 | 评审者 | 必需制品 | 人工门控 |
|---|------|---------|--------|---------|---------|
| 0 | **问题定义** | 研究者 | 评审者 | `problem_brief` | 否 |
| 1 | **文献综述** | 研究者 | 评审者 | `literature_map`, `evidence_table` | 否 |
| 2 | **假设形成** | 研究者 | 评审者 | `hypothesis_card` | **是** |
| 3 | **实验设计** | 工程师 | 评审者 | `experiment_spec` | 否 |
| 4 | **代码实现** | 工程师 | 评审者 | `code`, `test_result` | 否 |
| 5 | **实验执行** | 工程师 | 工程师 | `run_manifest`, `metrics` | **是** |
| 6 | **结果分析** | 研究者 | 评审者 | `result_report`, `claim_checklist` | 否 |

#### 状态转换

前进转换要求当前阶段的门控通过。后退转换（回滚）由评审者识别的特定失败类型触发。

```
前进（门控必须通过）:
  问题定义 ──▶ 文献综述 ──▶ 假设形成 ──▶ 实验设计 ──▶ 代码实现 ──▶ 实验执行 ──▶ 结果分析

后退（回滚）:
  假设形成 ◀── 文献综述         (need_more_evidence — 需要更多证据)
  实验设计 ◀── 假设形成         (hypothesis_needs_revision — 假设需要修订)
  代码实现 ◀── 实验设计         (design_flaw_found — 发现设计缺陷)
  实验执行 ◀── 代码实现         (code_bug_found — 发现代码缺陷)
  结果分析 ◀── 实验执行         (need_more_experiments — 需要更多实验)
  结果分析 ──▶ 假设形成         (hypothesis_falsified — 假设被证伪)
```

#### 修订循环

当门控失败时，同一智能体携带 Critic 的反馈被重新调度。这将持续到 `max_iterations`（默认：5）次后升级为人工干预。

```
 智能体产出制品
      │
      ▼
 评审者审查 ──── PASS ───▶ 推进到下一阶段
      │
    REVISE
      │
      ▼
 同一智能体携带反馈重新调度
      │
      ▼
 (重复最多 max_iterations 次)
      │
    FAIL / 次数耗尽
      │
      ▼
 跨阶段回滚 或 升级为人工处理
```

#### 语义化版本

每个事件都以语义化版本跟踪：`major.minor`，其中 `major` = 阶段索引（0-6），`minor` = 该阶段内的迭代次数，形成完整的审计追踪。

### 智能体角色

#### 研究者（Researcher）

研究者负责所有知识工作：文献分析、缺口识别、假设形成和结果解读。

**能力**：网络搜索、学术论文检索、结构化分析、生成子智能体  
**约束**：不能运行代码，不能设计实验  
**引用协议**：每篇论文必须有可验证的 URL。智能体信心不足 90% 的论文必须标记 `verified: false`。引用 5 篇真实论文好过引用 15 篇其中 10 篇是编造的。

**产出制品**：
- `problem_brief` — 研究问题、范围、动机、已有方法
- `literature_map` + `evidence_table` — 调研论文、关键发现、识别的缺口
- `hypothesis_card` — 可证伪的假设、终止标准、可测试的预测
- `result_report` + `claim_checklist` — 有证据支撑的结果分析

#### 工程师（Engineer）

工程师将假设转化为可执行的实验。

**能力**：代码编写、测试、调试、bash 执行、生成子智能体  
**约束**：不能修改假设，不能做质量判断  
**代码质量**：必须使用真实数据集（DummyDataset 直接判 FAIL），固定所有随机种子，对关键路径编写测试。

**草稿 vs 实际结果**：工程师产出*草稿*测试结果。编排器独立地将代码落盘、运行 `pytest` 并产出*已验证*的结果。评审者审查的是已验证的输出，而非草稿。

**产出制品**：
- `experiment_spec` — 完整的实验设计，含数据集、模型、指标、消融实验
- `code` + `test_result` — 通过测试的代码实现
- `run_manifest` + `metrics` — 执行清单和测量结果

#### 评审者（Critic）

评审者是对抗性审稿人。默认持怀疑态度，其职责是发现缺陷。

**能力**：完整项目沙箱访问（可读取所有内容）  
**约束**：不能写文件，不能创建制品，不能运行命令  
**判定输出**：结构化 YAML，包含 `PASS` / `REVISE` / `FAIL` 判定、各标准评分（0.0-1.0）、阻塞性问题列表，以及用于指导流水线回滚逻辑的 `failure_type` 分类。

**阶段特定检查**：
- *文献综述*：标记没有 URL 的论文；>50% 疑似编造 = 直接 FAIL
- *代码实现*：审查编排器产出的实际测试输出，而非智能体自报结果
- *实验执行*：所有指标都「恰好」超过目标 = 高度可疑（几乎肯定是伪造的）
- *结果分析*：没有具体实验证据的声明 = 直接 REVISE

#### 编排器（Orchestrator）

编排器是纯 Python 程序——不进行 LLM 调用。它管理状态、调度智能体、组装上下文、将代码落盘、独立运行测试，并解释门控结果。

**关键操作**：
1. 构建任务卡，包含前序制品的上下文和任何先前反馈
2. 通过配置的 CLI 后端调度合适的智能体
3. 解析输出并注册产出的制品
4. 调度评审者进行审查
5. 解释判定并路由（前进 / 修订 / 回滚 / 升级）
6. 追踪版本时间线、成本和状态转换

### 质量门控系统

每个阶段必须通过**三层质量门控**才能推进。

#### 第一层：Schema 验证（自动化）

每种制品类型都有 YAML Schema（位于 `schemas/`），定义必需字段、字段类型和最小列表长度。门控在 AI 评审之前验证结构正确性。

**已定义 14 种 Schema**：`problem_brief`、`literature_map`、`evidence_table`、`hypothesis_card`、`experiment_spec`、`code`、`test_result`、`run_manifest`、`metrics`、`result_report`、`claim_checklist`、`review_report`、`experiment_log`、`task_card`

#### 第二层：预审检查（领域特定）

在花费 token 进行 Critic 评审之前，捕获常见失败模式的结构性检查：

| 阶段 | 检查项 | 动作 |
|------|--------|------|
| 文献综述 | 验证论文 URL 是否可访问 | 标记幻觉引用 |
| 代码实现 | 检测 `DummyDataset` 使用 | 直接 FAIL |
| 代码实现 | 运行 pytest 并捕获结果 | 用实际输出覆盖草稿 |
| 实验执行 | 验证指标是否为真实数字 | 标记伪造结果 |
| 结果分析 | 检查 claim checklist 完成度 | <50% 则 REVISE |

#### 第三层：Critic 评审（AI 评估）

评审者对每个阶段按加权标准评分（权重之和为 1.0）。加权平均分必须达到 `pass_threshold`（默认：0.7）且无阻塞性问题。

**示例 — 问题定义阶段门控**：
| 标准 | 权重 | 描述 |
|------|------|------|
| 清晰度 | 0.20 | 问题陈述无歧义 |
| 重要性 | 0.20 | 解决真实且重要的缺口 |
| 范围 | 0.20 | 既不过宽也不过窄 |
| 新颖性 | 0.20 | 与已有工作有本质区别 |
| 可行性 | 0.20 | 可用现有资源进行研究 |

#### 失败类型路由

当评审者识别出失败时，`failure_type` 字段决定流水线的响应：

| failure_type | 含义 | 流水线动作 |
|---|---|---|
| `structural_issue` | Schema 违规、字段缺失 | 同阶段修订 |
| `implementation_bug` | 代码崩溃、测试失败 | 同阶段修订（代码实现） |
| `design_flaw` | 实验方案不完整或有误 | 回滚到实验设计 |
| `hypothesis_needs_revision` | 假设不可测试或过于模糊 | 回滚到假设形成 |
| `evidence_insufficient` | 数据点或实验不足 | 回滚到实验执行 |
| `hypothesis_falsified` | 结果证伪假设 | 回滚到假设形成 |
| `analysis_gap` | 声明缺乏证据支撑 | 同阶段修订（结果分析） |

### 制品通信机制

智能体**仅通过**版本化的 YAML 制品进行通信。没有共享内存，没有消息传递，没有智能体间的对话。

#### 生命周期

```
1. 编排器构建 TaskCard，包含前序制品的上下文
2. 智能体接收 TaskCard + 角色指令 (CLAUDE.md)
3. 智能体将制品 YAML 写入: projects/<id>/artifacts/<stage>/<type>_v<N>.yaml
4. 编排器在 ProjectState 中注册制品
5. 评审者接收制品进行审查
6. 修订时: 智能体在下一个 TaskCard 中接收 Critic 的反馈
```

#### 版本管理

制品遵循 `<type>_v<version>.yaml` 命名约定。版本在阶段内单调递增。所有制品都是不可变的历史记录——修订创建新版本，永不覆写。

### 存储架构

系统采用**基于文件、零数据库的持久化模型**。所有状态以 JSON 和 YAML 形式存储在本地文件系统中，不依赖任何外部数据库。本节详述五个存储层、数据模型、写入语义及其交互方式。

#### 第一层：ProjectState — 中央状态存储

每个项目有一个单一真相来源：`projects/<id>/state.json`。该文件是 JSON 序列化的 [Pydantic](https://docs.pydantic.dev/) `ProjectState` 模型，包含完整的项目生命周期数据。

**数据模型**（`src/research_agent/models.py`）：

```
ProjectState
├── project_id: str                    # Slug + UUID 后缀（如 "vla-model-0ddc306e"）
├── name: str                          # 项目名称
├── description: str
├── research_question: str             # 驱动研究的核心问题
├── current_stage: Stage               # 枚举：7 个阶段之一
├── artifacts: list[Artifact]          # 所有产出过的制品（只追加）
├── gate_results: list[GateResult]     # 所有门控评估结果（只追加）
├── transitions: list[StageTransition] # 阶段变更历史（只追加）
├── cost_records: list[CostRecord]     # 每次 API 调用的成本记录（只追加）
├── messages: list[AgentMessage]       # 智能体间消息（只追加）
├── timeline: list[VersionEvent]       # 语义版本化事件日志（只追加）
├── iteration_count: dict[str, int]    # 阶段名 → 当前迭代次数
├── config_overrides: dict             # 项目级配置覆盖
├── created_at: datetime
└── updated_at: datetime
```

**内嵌在 state.json 中的关键子模型：**

| 模型 | 核心字段 | 用途 |
|------|---------|------|
| `Artifact` | name, artifact_type, stage, version, path, created_by, metadata, provenance | 每个制品文件的注册表，含磁盘路径和创建溯源 |
| `GateResult` | gate_name, stage, status, checks[], reviewer, overall_feedback, iteration | 完整的门控评估结果，包含各标准评分和反馈文本 |
| `GateCheck` | name, check_type, passed, score, feedback | 门控内的单项检查（schema / codex / automated / human） |
| `StageTransition` | from_stage, to_stage, trigger, gate_result, timestamp | 每次前进推进或后退回滚的记录 |
| `CostRecord` | agent, provider, model, input_tokens, output_tokens, cost_usd, stage | 每次 LLM 调用的成本追踪，用于预算执行 |
| `VersionEvent` | version, event_type, agent, stage, summary, detail, gate_verdict, scores, cost_usd, duration_seconds | 语义版本化的审计事件，用于时间线展示 |

**只追加不变量**：`artifacts`、`gate_results`、`transitions`、`cost_records` 和 `timeline` 列表严格只追加。任何条目都不会被修改或删除。这使得 `state.json` 成为项目中所有事件的完整可审计日志——通过读取它可以精确重建智能体运行、门控评估、回滚和成本的完整序列。

**原子写入协议**（`state.py:_save_state`）：

```python
def _save_state(self, state: ProjectState) -> None:
    state_file = project_dir / "state.json"
    tmp_file   = project_dir / "state.json.tmp"
    # 1. 序列化到临时文件
    tmp_file.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    # 2. 原子重命名（POSIX 保证同文件系统上的 rename 是原子的）
    tmp_file.replace(state_file)
```

如果进程在步骤 1 崩溃，`state.json` 不受影响。如果在步骤 2 崩溃，重命名是原子的——文件要么是旧版本要么是新版本，永不会是部分写入。无需 WAL、日志或锁即可消除数据损坏。

**语义版本化**（`ProjectState.current_version`）：

```
Version = {stage_index}.{iteration_count}
示例: "2.3" = 第 2 阶段 (hypothesis_formation), 第 3 次迭代
```

版本号是计算得出的，不是存储的——从 `current_stage` 和 `iteration_count` 派生。时间线中的每个 `VersionEvent` 都记录这个版本号，形成人类可读的审计追踪。

#### 第二层：制品文件 — 智能体间通信通道

智能体**仅通过**磁盘上的版本化 YAML 文件进行通信。每个制品都有一个从类型和版本号推导的规范路径。

**存储布局：**

```
projects/<project-id>/
└── artifacts/
    ├── problem_definition/
    │   └── problem_brief_v1.yaml          # 研究者输出，迭代 1
    ├── literature_review/
    │   ├── literature_map_v1.yaml          # 首次尝试
    │   ├── literature_map_v2.yaml          # 评审反馈后修订
    │   └── evidence_table_v1.yaml
    ├── implementation/
    │   ├── code_v1.yaml                    # 工程师代码（YAML 包装）
    │   ├── test_result_v1.yaml             # 工程师草稿
    │   └── test_result_v2.yaml             # 编排器 **已验证** 结果
    └── ...
```

**规范路径推导**（`artifacts.py:create_artifact`）：

路径**始终**从 `artifact_type + 计算版本号` 推导，而非智能体写入的文件名。这防止了 state.json 记录版本 N 但磁盘文件是版本 M 的关键 bug 类别：

```python
canonical = f"{artifact_type.value}_v{version}.yaml"
rel_path  = f"artifacts/{stage.value}/{canonical}"
```

如果智能体写入非规范文件名，`register_artifact_file` 会将文件重命名以匹配。

**不可变性**：制品永不被覆写。修订总是创建 `_v{N+1}`。`state.latest_artifact(type)` 方法返回给定类型的最高版本制品，但所有先前版本保留在磁盘上作为历史记录。

**Schema 强制**（`schemas/*.schema.yaml`）：

14 种制品类型各有一个 YAML Schema，定义结构约束（必需字段、字段类型、最小列表长度）。门控系统在任何 AI 评审**之前**验证每个制品是否符合其 Schema，以零 token 成本捕获结构性失败。

#### 第三层：落盘代码 — 从 YAML 到可执行文件

`code` 制品较为特殊。它将源代码文件存储在 **YAML 包装器内部**：

```yaml
# artifacts/implementation/code_v1.yaml
files:
  - path: experiments/train.py
    content: |
      import torch
      from datasets import load_dataset
      ...
  - path: experiments/test_model.py
    content: |
      def test_forward_pass():
      ...
```

编排器将 YAML **落盘**为真实文件（`execution.py:materialize_code`）：

```
projects/<id>/experiments/train.py       ← 从 code_v1.yaml 提取
projects/<id>/experiments/test_model.py  ← 从 code_v1.yaml 提取
```

落盘后，编排器独立运行 `pytest` 和 `run_manifest` 中的冒烟测试。结果以**已验证**的 `test_result` 和 `metrics` 制品写回（带 `verified_by: orchestrator` 元数据），覆盖工程师产出的草稿制品。这是核心的防伪造机制——评审者审查的是编排器的已验证输出，而非工程师的自报结果。

#### 第四层：执行日志 — 原始进程输出

```
projects/<id>/logs/
├── problem_definition-a1b2c3.txt    # 智能体调度的原始 stdout/stderr
├── experiment_execution.txt          # 冒烟测试 / 实验输出
└── ...
```

日志文件捕获每个智能体子进程和实验执行的完整原始输出。它们仅用于参考——流水线不解析日志（判定解析使用结构化 YAML 输出），但对于调试失败不可或缺。

#### 第五层：配置文件 — 流水线行为

配置分布在两个人类可编辑的 YAML 文件中：

**`config/settings.yaml`** — 运行时配置（智能体后端/模型、流水线参数、成本限制）  
**`config/stages.yaml`** — 阶段定义（门控标准、回滚规则，结构性定义，很少变更）

Web GUI 可通过 `/api/config` REST 端点在运行时修改 `settings.yaml`，更改在下一个流水线步骤立即生效，无需重启。

#### 数据流总览

```
 ┌───────────────────────────────────────────────────────────────────────┐
 │                    config/settings.yaml + stages.yaml                │
 │                   （调度时读取，支持热重载）                             │
 └──────────────────────────────┬────────────────────────────────────────┘
                                │
                                ▼
 ┌──────────────────── Python 编排器 ────────────────────────────────────┐
 │  1. 加载 state.json                                                   │
 │  2. 读取配置 → 确定智能体、后端、模型                                    │
 │  3. 从前序制品组装上下文                                                │
 │  4. 构建 TaskCard (YAML)                                              │
 │  5. 调度智能体子进程                                                    │
 │     ├── 智能体将制品 YAML 写入 artifacts/<stage>/                       │
 │     └── 原始输出保存到 logs/                                           │
 │  6. 在 state.artifacts[] 中注册制品                                    │
 │  7. [实现阶段] 落盘代码 → experiments/                                  │
 │  8. [实现阶段] 运行 pytest → 写入已验证 test_result                     │
 │  9. 调度评审者 → 审查制品                                               │
 │ 10. 解析判定、评分、failure_type                                       │
 │ 11. 追加 GateResult 到 state.gate_results[]                           │
 │ 12. 追加 VersionEvent 到 state.timeline[]                             │
 │ 13. 追加 StageTransition 到 state.transitions[]                       │
 │ 14. 原子保存 state.json                                                │
 │ 15. 循环或推进                                                         │
 └───────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────┼───────────────┐
                    ▼           ▼               ▼
              state.json   artifacts/       experiments/
             （中央状态） （YAML 文件，    （落盘的源代码）
                          不可变，版本化）
```

#### 为什么选择基于文件而非数据库？

1. **Git 友好**：每次状态变更都是文件 diff。`git log` 即可查看项目历史，`git diff state.json` 精确显示变更内容
2. **零基础设施**：不需要 PostgreSQL、Redis 或 Docker。`pip install` 即可运行
3. **智能体兼容**：LLM 智能体原生支持 Read/Write 文件，无需 ORM、SQL 或 API 客户端
4. **可调试**：`cat state.json | jq '.timeline[-1]'` 查看最新事件，`cat artifacts/.../code_v1.yaml` 查看工程师写的完整代码，无需查询语言
5. **可移植**：复制 `projects/<id>/` 目录即拥有完整项目——状态、制品、代码、日志全在一处
6. **防崩溃**：原子 tmp+rename 写入在无事务的情况下防止数据损坏

### Web 图形界面

启动浏览器控制面板：

```bash
python scripts/multi_agent.py gui --port 8080
# 访问 http://localhost:8080
```

GUI 是一个**功能完备的单页应用**（暗色主题、响应式布局），无需命令行即可完整控制流水线。

#### 项目管理

- **项目侧边栏**：在持久化左侧面板中创建、切换和删除项目
- **项目创建弹窗**：设置项目名称和研究问题
- **多项目支持**：同时管理多个研究项目，各自拥有独立状态

#### 流水线控制

- **Auto / Step / Review** 按钮，配合实时状态指示器（空闲 / 运行中 / 等待审批）
- **Stop** 按钮可在执行中途取消操作
- **目标阶段选择器**：自动运行流水线到指定阶段
- **最大修订次数**：控制升级前的修订循环次数（1 / 2 / 3 / 5）
- **自定义指令**：注入每步指令以引导当前智能体
- **人工门控按钮**：流水线到达人工门控时自动出现 Approve / Reject 按钮，可附加反馈

#### 智能体配置面板

- **每个智能体独立配置卡片**：研究者、工程师、评审者、编排器
- **CLI 后端下拉框**：逐个智能体切换 `claude` / `codex` / `opencode`
- **模型下拉框**：根据所选后端动态填充可用模型
- **Effort 下拉框**：`low` / `medium` / `high` / `max` / `xhigh`（随后端变化）
- **即时保存**：更改在下一个流水线步骤立即生效，无需重启

#### 版本时间线

- **左侧面板**：按语义版本（`major.minor`）分组的可滚动时间线
- **阶段筛选下拉框**：按流水线阶段筛选时间线事件
- **事件图标**：按智能体角色着色（蓝=研究者，绿=工程师，紫=评审者，橙=人工）
- **点击展开**：选择任意版本在右侧面板查看完整事件详情

#### 事件详情面板

- **智能体标签**：显示哪个智能体产出了此事件
- **判定显示**：彩色编码 PASS（绿）/ REVISE（黄）/ FAIL（红）
- **评分标签**：各标准评分带通过/失败着色
- **制品链接**：点击在全屏查看器弹窗中查看完整制品内容
- **详情文本**：可展开/折叠的智能体输出，等宽字体
- **全部展开/折叠**：批量切换详情展示

#### 分析面板

- **各阶段成本条形图**：水平条形图显示每个流水线阶段的 USD 消耗
- **各阶段耗时条形图**：每个阶段花费的时间
- **各智能体成本分解**：每个角色消耗了多少预算
- **总成本和事件计数**显示在页头

#### 控制台

- **实时日志流**：2 秒轮询的智能体进程实时输出
- **错误高亮**：错误行以红色渲染
- **可调整大小**：拖拽调整控制台面板高度
- **自适应轮询**：运行时 2 秒，空闲时 8 秒

### 安装

#### 前置要求

- Python >= 3.11
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)（`claude --version`）
- [OpenAI Codex CLI](https://github.com/openai/codex)（`npm install -g @openai/codex && codex login`）
- [OpenCode CLI](https://github.com/opencode-ai/opencode)（可选，用于 Doubao/DeepSeek 后端）

#### 安装步骤

```bash
# 克隆仓库
git clone https://github.com/heeh02/research-agent.git
cd research-agent

# 可编辑模式安装
pip install -e .

# 安装开发依赖（用于测试）
pip install -e ".[dev]"

# 验证 CLI 工具
claude --version
codex --version
```

### 快速开始

#### 1. 创建项目

```bash
python scripts/pipeline.py init "VLA 模型效率优化" \
  -q "选择性注意力剪枝能否在不显著降低精度的情况下将 VLA 推理成本降低 40%？"
```

这将在 `projects/` 下创建项目目录并初始化 `state.json`。

#### 2. 运行流水线

**全自动模式**（自动推进所有阶段，在人工门控处暂停）：
```bash
python scripts/multi_agent.py auto
```

**逐步模式**（每次运行一个阶段，需要确认）：
```bash
python scripts/multi_agent.py step
```

**运行到指定阶段**：
```bash
python scripts/multi_agent.py auto --until hypothesis_formation
```

**Web GUI**（完整的可视化控制）：
```bash
python scripts/multi_agent.py gui --port 8080
```

#### 3. 查看状态

```bash
python scripts/multi_agent.py status
```

#### 4. 查看时间线

```bash
python scripts/multi_agent.py timeline
```

#### 运行模式

| 模式 | 命令 | 描述 |
|------|------|------|
| 全自动 | `python scripts/multi_agent.py auto` | 运行所有阶段，在人工门控处暂停 |
| 逐步 | `python scripts/multi_agent.py step` | 每次执行一个阶段 |
| 仅评审 | `python scripts/multi_agent.py review` | 对当前制品运行 Critic 评审 |
| Web GUI | `python scripts/multi_agent.py gui` | 完整的可视化控制面板 |
| 单智能体 | `python scripts/pipeline.py run` | 所有角色在一个进程中（快速探索） |

### 配置说明

所有配置位于 `config/settings.yaml`。也可通过 Web GUI 实时修改。

#### 智能体配置

```yaml
agents:
  researcher:
    backend: claude                          # CLI 后端: claude | codex | opencode
    model: claude-opus-4-20250514            # 模型（后端相关）
    effort: max                              # 等级: low | medium | high | max
    max_turns: 30                            # 最大对话轮数
    allowed_tools: Read,Write,Glob,Grep,WebSearch,WebFetch,Agent

  engineer:
    backend: opencode
    model: volcengine-plan/doubao-seed-2.0-code
    effort: high
    max_turns: 40
    allowed_tools: Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Agent

  critic:
    backend: codex
    model: gpt-5.4
    effort: xhigh
```

#### 流水线配置

```yaml
pipeline:
  automation_level: hybrid    # manual | hybrid | full
  human_gates:                # 需要人工审批的阶段
    - hypothesis_formation
    - experimentation
  max_iterations: 5           # 每阶段最大修订次数
  confirm_before_advance: true
```

#### 成本控制

```yaml
cost:
  warning_threshold: 5.0      # $5 时警告
  hard_limit: 50.0             # $50 时停止流水线
  codex_estimated_cost_per_review: 0.10
```

### 目录结构

```
research-agent/
├── agents/                      # 智能体角色指令
│   ├── researcher/CLAUDE.md     # 研究者角色指令与约束
│   ├── engineer/CLAUDE.md       # 工程师角色指令与约束
│   └── critic/CLAUDE.md         # 评审者角色指令与判定格式
├── config/                      # 配置文件
│   ├── settings.yaml            # 智能体后端、模型、流水线设置、成本限制
│   └── stages.yaml              # 阶段定义、门控标准、回滚规则
├── schemas/                     # 14 种制品类型的 YAML Schema
├── scripts/                     # 入口脚本
│   ├── multi_agent.py           # 主编排器 — auto, step, review, gui
│   ├── pipeline.py              # 状态管理 CLI — init, status, advance
│   └── setup.sh                 # 一键安装脚本
├── src/research_agent/          # 核心源码
│   ├── models.py                # Pydantic 模型、状态机、枚举
│   ├── state.py                 # 原子状态持久化 (JSON)
│   ├── artifacts.py             # Schema 验证、制品创建、上下文组装
│   ├── dispatcher.py            # 多后端智能体调度 (claude/codex/opencode)
│   ├── gates.py                 # 三层门控评估
│   ├── verdict.py               # 判定解析、加权评分、回滚路由
│   ├── prechecks.py             # 领域特定的预审检查
│   ├── execution.py             # 代码落盘、测试执行
│   ├── gui.py                   # Web GUI (SPA + REST API)
│   └── integrations/
│       └── codex.py             # Codex CLI 集成
├── tests/                       # 7 个测试模块覆盖所有核心逻辑
├── projects/                    # 项目工作空间（每个研究项目一个）
│   └── <project-id>/
│       ├── state.json           # 完整项目状态
│       ├── artifacts/<stage>/   # 版本化 YAML 制品
│       ├── implementations/     # 落盘的代码
│       └── logs/                # 执行日志
├── pyproject.toml
└── CLAUDE.md                    # 顶层编排器指令
```

### 测试

```bash
# 运行完整测试套件
pytest tests/ -v

# 运行特定测试模块
pytest tests/test_verdict.py -v

# 带覆盖率
pytest tests/ --cov=src/research_agent
```

关键测试不变量：
- `parse_verdict()` 永不默认返回 PASS——模糊输出总是返回 REVISE
- 缺失的门控分数计为 0.0——智能体无法通过省略分数绕过门控
- 状态机转换对照 `ALLOWED_TRANSITIONS` 进行验证
- 缺失字段的加权评分针对已知边界情况进行测试

## License

MIT
