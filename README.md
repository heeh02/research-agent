# Research Agent

A multi-agent system that automates the scientific research pipeline — from problem definition to result analysis — using isolated AI agents, structured quality gates, and human oversight.

Each agent runs as an independent CLI subprocess (Claude Code / OpenAI Codex / OpenCode). The orchestrator is pure Python: it manages the state machine, dispatches agents, verifies experiment results independently, and routes the pipeline based on structured critic feedback.

> **Status**: v0.2.0 — functional end-to-end pipeline with CLI and Web GUI. Used for robotics/VLA research projects. Not yet battle-tested across diverse domains.

```
Problem         Literature      Hypothesis     Experiment     Implementation  Experimentation   Analysis
Definition      Review          Formation      Design
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│Researcher│──▶│Researcher│──▶│Researcher│──▶│ Engineer │──▶│ Engineer │──▶│ Engineer │──▶│Researcher│
└────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘
     │              │              │              │              │              │              │
┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐
│  Critic  │   │  Critic  │   │  Critic  │   │  Critic  │   │  Critic  │   │  Critic  │   │  Critic  │
└────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘
     │              │              │              │              │              │              │
   [Gate]        [Gate]      [Gate+Human]      [Gate]        [Gate]      [Gate+Human]      [Gate]
```

## Table of Contents

- [Why This Exists](#why-this-exists)
- [What the System Actually Does](#what-the-system-actually-does)
- [Core Design Decisions](#core-design-decisions)
- [Architecture](#architecture)
- [The 7-Stage Pipeline](#the-7-stage-pipeline)
- [Quality Gate System](#quality-gate-system)
- [Agent Roles](#agent-roles)
- [Getting Started](#getting-started)
- [Running the Pipeline](#running-the-pipeline)
- [Web GUI](#web-gui)
- [Configuration](#configuration)
- [Repository Structure](#repository-structure)
- [Artifacts and State](#artifacts-and-state)
- [Limitations](#limitations)
- [Roadmap](#roadmap)

## Why This Exists

Research is hard to do rigorously. Researchers skip literature review, engineers cut corners on reproducibility, and nobody wants to be their own critic. This system separates concerns into isolated agents that enforce discipline:

- The **Researcher** handles literature analysis and hypothesis formation — it cannot run code
- The **Engineer** implements experiments — it cannot change the hypothesis
- The **Critic** reviews everything adversarially — it cannot modify artifacts
- The **Orchestrator** (pure Python) manages state — it independently verifies code execution and experiment results

Every stage requires passing a quality gate before the pipeline advances. When the gate fails, the system either revises (same stage) or rolls back (to an earlier stage), driven by the failure type identified by the critic.

## What the System Actually Does

Here is the end-to-end flow when you run `python scripts/multi_agent.py auto`:

1. **Orchestrator reads** the project state from `state.json` and determines the current stage
2. **Orchestrator builds a TaskCard** with the stage instruction, context from prior artifacts, and any previous critic feedback
3. **Orchestrator dispatches the appropriate agent** as a CLI subprocess (`claude -p`, `codex exec`, or `opencode run` depending on configuration)
4. **Agent writes YAML artifact(s)** to `projects/<id>/artifacts/<stage>/`
5. **Orchestrator validates** the output: parses YAML, checks against schema, registers with versioning
6. **For implementation/experimentation stages**: Orchestrator independently materializes code to disk, runs pytest and the smoke test, and writes *verified* test results that override the agent's draft
7. **Orchestrator runs structural pre-checks** (e.g., detect DummyDataset usage, check that papers have URL fields, flag suspiciously perfect metrics)
8. **Orchestrator dispatches the Critic** for adversarial review — the critic outputs a structured verdict (PASS/REVISE/FAIL) with per-criterion scores and a `failure_type`
9. **Three-layer gate evaluation**: critic verdict → pre-check override → weighted score check. Verdict can only be *downgraded*, never upgraded
10. **Routing decision**:
    - **PASS** → advance to next stage
    - **REVISE** (same-stage failure) → re-dispatch the agent with critic feedback
    - **FAIL** (cross-stage failure) → roll back to the stage identified by `failure_type`
    - **Human gate** (at hypothesis_formation and experimentation) → pause for manual approval
11. **Repeat** until the pipeline completes or exhausts revision attempts

## Core Design Decisions

| Decision | Why |
|----------|-----|
| **Agents are CLI subprocesses, not library calls** | True process isolation. Each agent has its own context, tools, and crash boundary. The orchestrator survives agent failures |
| **Communication via YAML files, not messages** | Artifacts on disk are auditable, versionable, and don't require shared memory. New agents can be added without protocol changes |
| **Verdict defaults to REVISE, never PASS** | If the critic's output can't be parsed, the safe default is to ask for revision. Missing scores count as 0.0. This prevents quality bypass |
| **Orchestrator independently verifies execution** | Agents can't self-report passing tests. The orchestrator materializes code and runs it, then writes its own verified artifact |
| **Failure types drive rollback routing** | Instead of generic "try again", the critic classifies *why* something failed (design_flaw, hypothesis_needs_revision, etc.), and the pipeline routes to the appropriate earlier stage |

## Architecture

```
Python Orchestrator (scripts/multi_agent.py or gui.py)
│
├── [Claude CLI]    →  Researcher Agent  ← agents/researcher/CLAUDE.md
│   Default: claude -p, Claude Opus 4
│   Tools: Read, Write, Glob, Grep, WebSearch, WebFetch, Agent
│
├── [OpenCode CLI]  →  Engineer Agent    ← agents/engineer/CLAUDE.md
│   Default: opencode run, Doubao Seed 2.0 Code
│   Tools: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Agent
│
├── [Codex CLI]     →  Critic Agent      ← agents/critic/CLAUDE.md
│   Default: codex exec, GPT-5.4
│   Access: Full project sandbox (read-only)
│
├── Orchestrator Execution (pure Python, no LLM)
│   - materialize code from YAML to disk
│   - run pytest and smoke tests
│   - parse metrics from output files and stdout
│   - write verified test_result and metrics artifacts
│
└── Human Gate
    Required at: hypothesis_formation, experimentation
```

**Backend flexibility**: Every agent's CLI backend and model can be independently configured via `config/settings.yaml` or the Web GUI. You can run all agents on Claude, mix Claude + Codex + OpenCode, or use entirely different models per role.

## The 7-Stage Pipeline

The pipeline is a finite state machine with 7 ordered stages. Each stage has a primary agent, required artifacts, and weighted gate criteria.

| # | Stage | Primary | Reviewer | Required Artifacts | Human Gate |
|---|-------|---------|----------|-------------------|------------|
| 0 | Problem Definition | Researcher | Critic | `problem_brief` | No |
| 1 | Literature Review | Researcher | Critic | `literature_map`, `evidence_table` | No |
| 2 | Hypothesis Formation | Researcher | Critic | `hypothesis_card` | **Yes** |
| 3 | Experiment Design | Engineer | Critic | `experiment_spec` | No |
| 4 | Implementation | Engineer | Critic | `code`, `test_result` | No |
| 5 | Experimentation | Engineer | Engineer | `run_manifest`, `metrics` | **Yes** |
| 6 | Analysis | Researcher | Critic | `result_report`, `claim_checklist` | No |

### State Transitions

Forward transitions require the gate to pass. Backward transitions (rollbacks) are triggered by specific failure types:

```
Forward (gate pass required):
  problem_definition → literature_review → hypothesis_formation → experiment_design
    → implementation → experimentation → analysis

Backward (failure-type driven):
  hypothesis_formation  ← literature_review       (need_more_evidence)
  experiment_design     ← hypothesis_formation     (hypothesis_needs_revision)
  implementation        ← experiment_design        (design_flaw_found)
  experimentation       ← implementation           (code_bug_found)
  analysis              ← experimentation          (need_more_experiments)
  analysis              → hypothesis_formation     (hypothesis_falsified)
```

### Revision Cycles

When a gate fails with a same-stage failure type, the agent is re-dispatched with the critic's feedback. This repeats up to `max_iterations` (default: 5) before escalating. Every iteration is tracked with semantic versioning: `major.minor` where `major` = stage index (0-6) and `minor` = iteration count.

## Quality Gate System

Every stage must pass a **three-layer quality gate** before the pipeline advances. Each layer can only *downgrade* the verdict, never upgrade it.

### Layer 1: Critic Review

The Critic agent scores each stage against weighted criteria (defined in `config/stages.yaml`). The weighted average must meet `pass_threshold` (default: 0.7) with no blocking issues.

Example — Problem Definition gate criteria:

| Criterion | Weight | Description |
|-----------|--------|-------------|
| Clarity | 0.20 | Problem statement is unambiguous |
| Significance | 0.20 | Addresses a real and important gap |
| Scope | 0.20 | Neither too broad nor too narrow |
| Novelty | 0.20 | Genuinely different from existing work |
| Feasibility | 0.20 | Can be investigated with available resources |

The critic also outputs a `failure_type` (one of 7 types) that determines whether the pipeline revises in place or rolls back.

### Layer 2: Structural Pre-Checks

Automated checks that catch common failure modes before critic review:

| Stage | Check | What It Does |
|-------|-------|-------------|
| Literature Review | Paper URL presence | Flags papers missing the `url` field |
| Implementation | DummyDataset detection | Automatic REVISE if code uses dummy/random data |
| Implementation | Orchestrator test execution | Runs pytest independently, writes verified test_result |
| Experimentation | Metrics fabrication detection | Flags if all metrics "just barely" exceed every target |
| Analysis | Claim checklist completion | REVISE if < 50% of claims are validated |

If pre-checks find blocking issues and the critic said PASS, the verdict is overridden to REVISE.

### Layer 3: Weighted Score Check

If the critic said PASS but the weighted average of scores is below the threshold, the verdict is overridden to REVISE. Missing score fields count as 0.0 — agents cannot bypass the gate by omitting low-scoring criteria.

## Agent Roles

### Researcher

Handles all knowledge work: literature analysis, gap identification, hypothesis formation, result interpretation.

- **Tools**: Web search, paper retrieval, structured analysis, subagent spawning
- **Constraints**: Cannot run code, cannot design experiments (when using Claude backend with `--allowedTools`)
- **Citation rule**: Every paper must have a `url` field. Papers the agent is uncertain about must be marked `verified: false`
- **Produces**: `problem_brief`, `literature_map`, `evidence_table`, `hypothesis_card`, `result_report`, `claim_checklist`

### Engineer

Translates hypotheses into executable experiments.

- **Tools**: Code writing, testing, debugging, bash execution, subagent spawning
- **Constraints**: Cannot change the hypothesis, cannot make quality judgments
- **Code rule**: Must use real datasets (DummyDataset is an automatic FAIL), pin random seeds, include tests
- **Draft vs Verified**: Engineer produces *draft* test results. The orchestrator independently materializes code, runs tests, and writes the *verified* artifact that the critic reviews
- **Produces**: `experiment_spec`, `code`, `test_result`, `run_manifest`, `metrics`

### Critic

Adversarial reviewer. Skeptical by default.

- **Access**: Full project sandbox (read-only)
- **Constraints**: Cannot write files, cannot create artifacts, cannot run commands
- **Output**: Structured YAML with verdict (PASS/REVISE/FAIL), per-criterion scores (0.0-1.0), blocking issues, and `failure_type` for rollback routing
- **Produces**: Review output (stdout only, not written to files)

### Orchestrator

The Python code itself — not an LLM agent.

- Builds task cards with context from prior artifacts and previous feedback
- Dispatches agents via configured CLI backend
- Validates and registers produced artifacts
- Independently materializes code and runs tests (implementation/experimentation)
- Evaluates gate verdicts through the three-layer system
- Routes pipeline: advance / revise / rollback / human escalation
- Tracks version timeline, costs, and transitions

## Getting Started

### Prerequisites

- Python 3.11+
- At least one CLI backend installed:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude --version`)
  - [OpenAI Codex](https://github.com/openai/codex) (`npm install -g @openai/codex && codex login`) — used for critic reviews
  - [OpenCode](https://github.com/opencode-ai/opencode) (optional, for cost-effective engineering)

### Installation

```bash
git clone <repo-url> && cd research-agent
pip install -e .
```

### Quick Start

```bash
# 1. Create a project
ra init "My Research" -q "Does selective attention pruning reduce VLA inference latency?"

# 2. Run the full pipeline (auto mode)
python scripts/multi_agent.py auto

# 3. Or use the Web GUI
python scripts/multi_agent.py gui
# Open http://127.0.0.1:8080
```

## Running the Pipeline

### Fully Automated (auto mode)

```bash
python scripts/multi_agent.py auto                    # Run until completion or human gate
python scripts/multi_agent.py auto --until hypothesis_formation  # Stop at a specific stage
python scripts/multi_agent.py auto -n 5               # Allow up to 5 revisions per stage
```

The pipeline runs autonomously, pausing only at human gates (hypothesis_formation, experimentation) which require `ra advance --approve`.

### Step-by-Step

```bash
python scripts/multi_agent.py step                    # Run one agent + one review
python scripts/multi_agent.py step -i "Focus on transformer-based approaches"
python scripts/multi_agent.py review                  # Run critic review only
```

### State Management (`ra` CLI)

```bash
ra status                   # Show pipeline state and progress
ra advance --approve        # Approve a human gate
ra rollback literature_review --reason "Need more papers"
ra cost                     # Show cost breakdown by stage/agent/model
ra artifacts                # List all artifacts
ra history                  # Show stage transitions
ra projects                 # List all projects
ra use <project-id>         # Switch active project
```

### Timeline

```bash
python scripts/multi_agent.py timeline    # Print version timeline with all events
```

## Web GUI

Launch with `python scripts/multi_agent.py gui` (default: http://127.0.0.1:8080).

The GUI provides:

- **Project sidebar**: Create, switch, and delete projects
- **Pipeline control**: Auto, Step, Review, Stop, Approve/Reject buttons
- **Stage visualization**: Progress bar with iteration counts and gate verdicts
- **Version timeline**: Every agent run, gate evaluation, rollback, and human decision
- **Detail panel**: Full output for each event, with artifact viewer
- **Settings panel**: Change CLI backend, model, and effort per agent at runtime
- **Console**: Real-time log output with search
- **Stats**: Cost and duration analytics per stage and per agent
- **Browser notifications**: Alerts when human approval is needed or pipeline finishes

Settings changes apply to the next pipeline step without restart.

## Configuration

All configuration lives in `config/settings.yaml`:

```yaml
agents:
  researcher:
    backend: claude                           # claude | codex | opencode
    model: claude-opus-4-20250514
    effort: max
    max_turns: 30
    allowed_tools: Read,Write,Glob,Grep,WebSearch,WebFetch,Agent
  engineer:
    backend: opencode
    model: volcengine-plan/doubao-seed-2.0-code
    effort: high
    max_turns: 40
  critic:
    backend: codex
    model: gpt-5.4
    effort: xhigh

pipeline:
  human_gates: [hypothesis_formation, experimentation]
  max_iterations: 5
  automation_level: hybrid

cost:
  warning_threshold: 5.0
  hard_limit: 50.0
```

Stage-specific gate criteria and weights are in `config/stages.yaml`. Each stage defines:
- Required artifacts
- Weighted gate criteria (summing to 1.0)
- Pass threshold (default: 0.7)
- Primary agent and reviewer
- Whether a human gate is required

## Repository Structure

```
research-agent/
├── scripts/
│   ├── multi_agent.py          # Main orchestrator: auto, step, review, gui, timeline
│   ├── pipeline.py             # Low-level state CLI: init, save, validate, repair
│   ├── codex_review.py         # Standalone Codex review script
│   └── gpt_review.py           # Deprecated GPT API fallback
│
├── src/research_agent/
│   ├── models.py               # Type system: Stage, AgentRole, ProjectState, FSM transitions
│   ├── state.py                # Persistence: atomic write, flock, project CRUD
│   ├── artifacts.py            # Schema validation, artifact registration, context assembly
│   ├── dispatcher.py           # Multi-backend dispatch: claude, codex, opencode subprocess mgmt
│   ├── verdict.py              # Verdict parsing, weighted scoring, rollback routing
│   ├── gate_eval.py            # Three-layer gate evaluation (composition layer)
│   ├── prechecks.py            # Domain-specific structural checks per stage
│   ├── execution.py            # Code materialization, test execution, verified artifact writing
│   ├── sandbox.py              # Snapshot-diff violation detection for agent isolation
│   ├── gui.py                  # Web GUI: PipelineRunner, HTTP server, embedded SPA
│   ├── cli.py                  # `ra` CLI (Click + Rich)
│   ├── agents/
│   │   ├── critic.py           # CriticAgent class, stage review criteria
│   │   └── __init__.py
│   └── integrations/
│       ├── codex.py            # Codex CLI integration (exec, parsing)
│       ├── llm.py              # Cost estimation pricing table
│       ├── github.py           # GitHub integration (disabled by default)
│       └── tracking.py         # MLflow/W&B tracking (disabled by default)
│
├── agents/                     # Agent role instructions (CLAUDE.md per role)
│   ├── researcher/CLAUDE.md
│   ├── engineer/CLAUDE.md
│   └── critic/CLAUDE.md
│
├── config/
│   ├── settings.yaml           # Agent backends, models, pipeline config, cost limits
│   ├── stages.yaml             # Stage definitions, gate criteria, rollback rules
│   └── agents.yaml             # Agent role contracts (documentation)
│
├── schemas/                    # 14 YAML schemas for artifact validation
│   ├── problem_brief.schema.yaml
│   ├── hypothesis_card.schema.yaml
│   ├── code.schema.yaml
│   └── ...
│
├── templates/                  # Artifact templates
├── tests/                      # Unit tests (3000+ lines, 9 modules)
│
└── projects/                   # Runtime: per-project state, artifacts, logs
    └── <project-id>/
        ├── state.json          # Complete project state (atomic writes)
        ├── artifacts/<stage>/  # Versioned YAML artifacts
        ├── experiments/        # Materialized code files
        └── logs/               # Agent output logs
```

## Artifacts and State

### Artifact Communication

Agents communicate **exclusively** through versioned YAML files. There is no shared memory and no conversation between agents.

Artifacts follow the naming convention `<type>_v<version>.yaml`. Versions are monotonically increasing. All artifacts are immutable — revisions create new versions, never overwrite.

The orchestrator assembles context from prior artifacts into each TaskCard, so each agent receives the cumulative knowledge of the pipeline without needing to see other agents' raw output.

### State Persistence

Each project has a single source of truth: `projects/<id>/state.json`, a JSON-serialized Pydantic model containing:

- Current stage and iteration count
- All artifacts (append-only registry)
- All gate results (append-only)
- Stage transitions (forward advances and backward rollbacks)
- Cost records (per-call API cost tracking)
- Version timeline (semantic-versioned event log)

**Write safety**: State writes use exclusive file locking (`fcntl.flock`) plus atomic tmp-file + rename. If the process crashes, state.json is either the old version or the new version, never a partial write.

## Limitations

This section describes what the system **does not** do, or does imperfectly.

### Agent Isolation is Soft

Agent tool restriction (`--allowedTools`) only works with the Claude backend. When using OpenCode or Codex, agents have access to all tools. The sandbox module detects violations after the fact (via filesystem snapshot comparison) but does not prevent them.

### No Execution Sandboxing

The orchestrator runs agent-authored code (pytest, smoke tests) directly in the host environment. There is no Docker container or VM isolation. The command allowlist (`python`, `pytest`, `pip` only, no shell operators) mitigates but does not eliminate risk.

### URL Verification is Structural Only

The pre-check for literature review verifies that papers have a `url` field in the YAML — it does not check whether the URL is actually reachable.

### Independent Verification is Per-Stage

Only the implementation and experimentation stages have orchestrator-verified execution. For other stages (problem definition, literature review, hypothesis, analysis), the agent's output is the final artifact, reviewed by the critic but not independently reproduced.

### State Growth

`state.json` uses append-only lists for artifacts, gate results, transitions, and timeline events. For projects with many revision cycles, this file will grow. There is currently no compaction or archival mechanism.

### Single-Machine

The system runs on one machine. There is no distributed dispatch, no job queue, and no cloud execution backend.

## Roadmap

Near-term priorities (roughly in order):

- [ ] Extract shared `PipelineEngine` to eliminate CLI/GUI logic duplication
- [ ] Move GUI frontend (HTML/CSS/JS) to separate static files
- [ ] Strengthen command validation in execution.py
- [ ] Add HTTP HEAD verification for literature review URLs
- [ ] State compaction for long-running projects
- [ ] Docker-based execution sandbox for agent-authored code
- [ ] Consolidate CLI entry points (pipeline.py → ra CLI)

Research directions:

- Formal verification of the verdict monotonicity property
- Cross-domain benchmarking (beyond robotics/VLA)
- Multi-machine agent dispatch with job queue
- Integration with experiment tracking (MLflow/W&B scaffolding exists)

---

## 中文概述

Research Agent 是一个多智能体自动化科研系统。它将研究流程拆分为 7 个阶段（问题定义 → 文献综述 → 假设形成 → 实验设计 → 代码实现 → 实验执行 → 结果分析），每个阶段由专职 AI agent 执行，经过对抗性评审后才能推进。

**核心特点**：
- **进程级隔离**：每个 agent 是独立的 CLI 子进程（Claude / Codex / OpenCode），不共享内存
- **三层质量门控**：critic 评审 → 结构化预检查覆盖 → 加权分数覆盖，verdict 只能降级不能升级
- **独立验证**：Orchestrator 独立落盘代码并运行测试，agent 不能伪造结果
- **故障类型驱动回退**：critic 输出结构化 failure_type，pipeline 自动回退到正确的阶段
- **可配置 backend**：每个 agent 的 CLI backend 和模型可独立配置，支持 Claude、Codex、OpenCode

**快速开始**：
```bash
pip install -e .
ra init "研究课题" -q "研究问题"
python scripts/multi_agent.py auto    # 全自动运行
python scripts/multi_agent.py gui     # Web GUI
```
