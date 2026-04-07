# Research Agent

A **multi-agent automated research system** that orchestrates isolated AI agents through a structured 7-stage scientific pipeline — from problem definition to result analysis — with built-in quality gates, adversarial review, and human oversight.

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

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Core Principles](#core-principles)
- [Architecture](#architecture)
- [The Pipeline](#the-pipeline)
- [Agent Roles](#agent-roles)
- [Quality Gate System](#quality-gate-system)
- [Artifact Communication](#artifact-communication)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Web GUI](#web-gui)
- [Directory Structure](#directory-structure)
- [Testing](#testing)

## Why This Exists

Running rigorous research is hard. Researchers skip literature review, engineers cut corners on reproducibility, and nobody wants to be their own critic. This system enforces the scientific method by separating concerns into isolated agents that **cannot** bypass each other:

- The **Researcher** cannot run code (preventing "just try it" shortcuts)
- The **Engineer** cannot change the hypothesis (preventing goalpost-moving)
- The **Critic** cannot rewrite artifacts (preventing conflicts of interest)
- The **Orchestrator** only manages state (preventing unauthorized LLM calls)

Every artifact is reviewed. Every transition is gated. Every result is independently verified.

## Core Principles

| Principle | Implementation |
|-----------|---------------|
| **Separation of Concerns** | Each agent has a strict toolset — the Researcher can't run code, the Engineer can't change hypotheses, the Critic can't write artifacts |
| **Artifact-Based Communication** | Agents never talk directly. All state flows through versioned YAML artifacts on disk |
| **Adversarial Review** | Every stage is reviewed by a Critic agent that is incentivized to find flaws, not approve work |
| **Independent Verification** | The Orchestrator materializes code and runs tests independently — agents cannot fabricate results |
| **Conservative Defaults** | Verdict parser defaults to REVISE (never PASS). Missing scores count as 0.0. Pre-checks can override the critic |
| **Human-in-the-Loop** | Configurable human gates at critical decision points (hypothesis, experimentation) |
| **Atomic Persistence** | State writes use tmp+rename to prevent corruption on crash |

## Architecture

```
Python Orchestrator (scripts/multi_agent.py)
│
├── claude -p ──────── Researcher Agent ──── agents/researcher/CLAUDE.md
│   Tools: Read, Write, Glob, Grep, WebSearch, WebFetch, Agent
│   Model: Claude Opus 4 (configurable)
│
├── opencode run ───── Engineer Agent ────── agents/engineer/CLAUDE.md
│   Tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Agent
│   Model: Doubao Seed 2.0 Code (configurable)
│
├── codex exec ─────── Critic Agent ──────── agents/critic/CLAUDE.md
│   Access: Full project sandbox (reads everything)
│   Model: GPT-5.4 (configurable)
│
└── Human Gate ─────── Manual Approval
    Stages: hypothesis_formation, experimentation
```

### CLI Backends

The system supports three CLI backends, each dispatching agents as independent subprocesses:

| Backend | Command | Use Case |
|---------|---------|----------|
| **Claude** | `claude -p` | Claude Code CLI — supports Opus/Sonnet with tool restrictions |
| **Codex** | `codex exec` | OpenAI Codex — full sandbox access, ideal for adversarial review |
| **OpenCode** | `opencode run` | OpenCode CLI — supports Doubao, DeepSeek, Kimi, and other models |

## The Pipeline

### 7 Stages

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

### State Transitions

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

### Revision Cycles

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

### Semantic Versioning

Every event is tracked with semantic versioning: `major.minor` where `major` = stage index (0-6) and `minor` = iteration count within that stage. This provides a complete audit trail.

## Agent Roles

### Researcher (Claude Opus)

The Researcher handles all knowledge work: literature analysis, gap identification, hypothesis formation, and result interpretation.

**Capabilities**: Web search, academic paper retrieval, structured analysis  
**Constraints**: Cannot run code, cannot design experiments  
**Citation Protocol**: Every paper must have a verifiable URL. Papers the agent is less than 90% confident exist must be marked `verified: false`. It is better to cite 5 real papers than 15 with 10 fabricated.

**Produced Artifacts**:
- `problem_brief` — Research problem, scope, motivation, existing approaches
- `literature_map` + `evidence_table` — Surveyed papers, key findings, identified gaps
- `hypothesis_card` — Falsifiable claim, kill criteria, testable predictions
- `result_report` + `claim_checklist` — Analysis with evidence-backed claims

### Engineer (Doubao/OpenCode)

The Engineer translates hypotheses into executable experiments.

**Capabilities**: Code writing, testing, debugging, bash execution  
**Constraints**: Cannot change the hypothesis, cannot make quality judgments  
**Code Quality**: Must use real datasets (DummyDataset is an automatic FAIL), pin all random seeds, include tests for critical paths.

**Draft vs. Actual Results**: The Engineer produces *draft* test results. The Orchestrator independently materializes code to disk, runs `pytest`, and produces *verified* results. The Critic reviews the verified output, not the draft.

**Produced Artifacts**:
- `experiment_spec` — Complete experimental design with datasets, models, metrics, ablations
- `code` + `test_result` — Implementation with passing tests
- `run_manifest` + `metrics` — Execution manifest and measured results

### Critic (Codex/GPT-5.4)

The Critic is an adversarial reviewer. It is skeptical by default and incentivized to find flaws.

**Capabilities**: Full project sandbox access (reads everything)  
**Constraints**: Cannot write files, cannot create artifacts, cannot run commands  
**Verdict**: Outputs structured YAML with `PASS` / `REVISE` / `FAIL`, per-criterion scores (0.0-1.0), blocking issues, and a classified `failure_type` that routes the pipeline's rollback logic.

**Stage-Specific Checks**:
- *Literature Review*: Flags papers without URLs; >50% suspected fabrication = automatic FAIL
- *Implementation*: Reviews actual test output from Orchestrator, not agent self-reports
- *Experimentation*: Metrics that "just barely" exceed all targets = HIGHLY suspicious
- *Analysis*: Claims without specific experimental evidence = automatic REVISE

### Orchestrator (Python)

The Orchestrator is pure Python — no LLM calls. It manages state, dispatches agents, assembles context, materializes code, runs tests independently, and interprets gate results.

**Key Operations**:
1. Build task cards with context from prior artifacts and any previous feedback
2. Dispatch the appropriate agent via the configured CLI backend
3. Parse output and register produced artifacts
4. Dispatch the Critic for review
5. Interpret the verdict and route (advance / revise / rollback / escalate)
6. Track version timeline, costs, and transitions

## Quality Gate System

Every stage must pass a **three-layer quality gate** before the pipeline advances.

### Layer 1: Schema Validation (Automated)

Each artifact type has a YAML schema (in `schemas/`) defining required fields, field types, and minimum list lengths. The gate verifies structural correctness before any AI review.

**14 schemas defined**: `problem_brief`, `literature_map`, `evidence_table`, `hypothesis_card`, `experiment_spec`, `code`, `test_result`, `run_manifest`, `metrics`, `result_report`, `claim_checklist`, `review_report`, `experiment_log`, `task_card`

### Layer 2: Pre-Review Checks (Domain-Specific)

Structural checks that catch common failure modes before spending tokens on critic review:

| Stage | Check | Action |
|-------|-------|--------|
| Literature Review | Verify paper URLs are reachable | Flag hallucinated citations |
| Implementation | Detect `DummyDataset` usage | Automatic FAIL |
| Implementation | Run pytest and capture results | Override draft with actual output |
| Experimentation | Validate metrics are real numbers | Flag fabricated results |
| Analysis | Check claim checklist completion | REVISE if < 50% complete |

### Layer 3: Critic Review (AI Evaluation)

The Critic scores each stage against weighted criteria (all summing to 1.0). The weighted average must meet `pass_threshold` (default: 0.7) with no blocking issues.

**Example — Problem Definition Gate**:
| Criterion | Weight | Description |
|-----------|--------|-------------|
| Clarity | 0.20 | Problem statement is unambiguous |
| Significance | 0.20 | Addresses a real and important gap |
| Scope | 0.20 | Neither too broad nor too narrow |
| Novelty | 0.20 | Genuinely different from existing work |
| Feasibility | 0.20 | Can be investigated with available resources |

### Failure Type Routing

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

## Artifact Communication

Agents communicate **exclusively** through versioned YAML artifacts. There is no shared memory, no message passing, and no conversation between agents.

### Lifecycle

```
1. Orchestrator builds TaskCard with context from prior artifacts
2. Agent receives TaskCard + role instructions (CLAUDE.md)
3. Agent writes artifact YAML to: projects/<id>/artifacts/<stage>/<type>_v<N>.yaml
4. Orchestrator registers artifact in ProjectState
5. Critic receives artifact for review
6. On revision: agent receives Critic feedback in next TaskCard
```

### Versioning

Artifacts follow the naming convention `<type>_v<version>.yaml`. Versions are monotonically increasing within a stage. All artifacts are immutable historical records — revisions create new versions, never overwrite.

### Example Artifact (hypothesis_card)

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

## Installation

### Prerequisites

- Python >= 3.11
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude --version`)
- [OpenAI Codex CLI](https://github.com/openai/codex) (`npm install -g @openai/codex && codex login`)
- [OpenCode CLI](https://github.com/opencode-ai/opencode) (optional, for Doubao/DeepSeek backends)

### Install

```bash
# Clone the repository
git clone https://github.com/your-org/research-agent.git
cd research-agent

# Install in editable mode
pip install -e .

# Install dev dependencies (for testing)
pip install -e ".[dev]"

# Verify CLI tools
claude --version
codex --version
```

## Quick Start

### 1. Create a Project

```bash
python scripts/pipeline.py init "VLA Model Efficiency" \
  -q "Can selective attention pruning reduce VLA inference cost by 40% without significant accuracy loss?"
```

This creates a project directory under `projects/` with an initialized `state.json`.

### 2. Run the Pipeline

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

### 3. Check Status

```bash
python scripts/multi_agent.py status
```

### 4. View Timeline

```bash
python scripts/multi_agent.py timeline
```

### Operating Modes

| Mode | Command | Description |
|------|---------|-------------|
| Full Auto | `python scripts/multi_agent.py auto` | Runs all stages, pauses at human gates |
| Step-by-Step | `python scripts/multi_agent.py step` | One stage per invocation |
| Review Only | `python scripts/multi_agent.py review` | Run Critic on current artifacts |
| Web GUI | `python scripts/multi_agent.py gui` | Browser-based control panel |
| Single-Agent | `python scripts/pipeline.py run` | All roles in one process (for quick exploration) |

## Configuration

All configuration lives in `config/settings.yaml`.

### Agent Configuration

```yaml
agents:
  researcher:
    backend: claude                          # CLI backend: claude | codex | opencode
    model: claude-opus-4-20250514            # Model to use
    effort: max                              # Effort level: low | medium | high | max
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

### Pipeline Configuration

```yaml
pipeline:
  automation_level: hybrid    # manual | hybrid | full
  human_gates:                # Stages requiring human approval
    - hypothesis_formation
    - experimentation
  max_iterations: 5           # Max revision cycles per stage
  confirm_before_advance: true
```

### Cost Controls

```yaml
cost:
  warning_threshold: 5.0      # Warn at $5 spent
  hard_limit: 50.0             # Stop pipeline at $50
  codex_estimated_cost_per_review: 0.10
```

### Gate Criteria

Each stage defines weighted criteria in `config/stages.yaml`. Example:

```yaml
stages:
  hypothesis_formation:
    gate_criteria:
      - name: falsifiability
        description: "Can be disproven by experiment"
        weight: 0.20
      - name: novelty
        description: "Not a minor variation of existing work"
        weight: 0.15
      - name: testability
        description: "Minimum viable experiment can test this"
        weight: 0.20
      # ...
    pass_threshold: 0.7
    human_gate: true
```

## Web GUI

Launch the browser-based control panel:

```bash
python scripts/multi_agent.py gui --port 8080
# Visit http://localhost:8080
```

Features:
- Project creation and switching
- Pipeline control (auto, step, review) with real-time log streaming
- Per-agent CLI backend selection
- Version timeline visualization with drill-down
- Cost tracking dashboard

## Directory Structure

```
research-agent/
├── agents/
│   ├── researcher/CLAUDE.md     # Researcher role instructions & constraints
│   ├── engineer/CLAUDE.md       # Engineer role instructions & constraints
│   └── critic/CLAUDE.md         # Critic role instructions & verdict format
├── config/
│   ├── settings.yaml            # Agent models, tools, pipeline settings, cost limits
│   └── stages.yaml              # Stage definitions, gate criteria, rollback rules
├── schemas/                     # YAML schemas for all 14 artifact types
│   ├── problem_brief.schema.yaml
│   ├── hypothesis_card.schema.yaml
│   ├── code.schema.yaml
│   └── ...
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
│   ├── gui.py                   # Web GUI (HTTP server + REST API)
│   └── integrations/
│       └── codex.py             # Codex CLI integration for critic reviews
├── tests/                       # Comprehensive test suite
│   ├── test_models.py           # State machine, transitions, data models
│   ├── test_artifacts.py        # Schema validation, artifact creation
│   ├── test_dispatcher.py       # Task card building, agent result parsing
│   ├── test_state.py            # Project persistence, atomic writes
│   ├── test_verdict.py          # Verdict parsing, scoring, rollback routing
│   ├── test_prechecks.py        # Pre-review structural checks
│   └── test_execution.py        # Code materialization, test execution
├── projects/                    # Project workspaces (one per research project)
│   └── <project-id>/
│       ├── state.json           # Complete project state
│       ├── artifacts/<stage>/   # Versioned YAML artifacts
│       ├── implementations/     # Materialized code
│       └── logs/                # Execution logs
├── pyproject.toml               # Package definition (hatchling)
└── CLAUDE.md                    # Top-level orchestrator instructions
```

## Testing

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

## License

MIT
