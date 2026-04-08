# Research Agent

[English](#table-of-contents) | [中文](#中文文档)

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

<a id="中文"></a>

## 中文文档

### 项目简介

Research Agent 是一个多智能体自动化科研系统，通过隔离的 AI agent、结构化质量门控和人类监督，自动化从问题定义到结果分析的完整科研流程。

每个 agent 以独立的 CLI 子进程运行（Claude Code / OpenAI Codex / OpenCode）。Orchestrator 是纯 Python 代码：管理状态机、调度 agent、独立验证实验结果，并根据 critic 的结构化反馈路由 pipeline。

> **状态**：v0.2.0 — 可用的端到端 pipeline，支持 CLI 和 Web GUI。已用于机器人/VLA 研究项目，尚未在多领域大规模验证。

### 为什么做这个项目

做严谨的研究很难：研究者跳过文献综述，工程师在可复现性上偷工减料，没人愿意当自己的审稿人。这个系统通过职责分离来强制纪律：

- **Researcher**（研究员）负责文献分析和假设形成 — 不能运行代码
- **Engineer**（工程师）负责实验实现 — 不能修改假设
- **Critic**（审稿人）对抗性审查一切 — 不能修改产物
- **Orchestrator**（编排器，纯 Python）管理状态 — 独立验证代码执行和实验结果

每个阶段必须通过质量门控才能推进。门控失败时，系统要么修订（同阶段），要么回退（到更早阶段），由 critic 识别的故障类型驱动。

### 系统实际做什么

运行 `python scripts/multi_agent.py auto` 后的端到端流程：

1. **Orchestrator 读取**项目状态 `state.json`，确定当前阶段
2. **构建 TaskCard**：组装阶段指令、前序产物上下文、之前的 critic 反馈
3. **调度对应 agent**：启动 CLI 子进程（`claude -p`、`codex exec` 或 `opencode run`，取决于配置）
4. **Agent 写入 YAML 产物**到 `projects/<id>/artifacts/<stage>/`
5. **Orchestrator 验证输出**：解析 YAML、校验 schema、带版本号注册
6. **实现/实验阶段额外步骤**：Orchestrator 独立将代码从 YAML 落盘、运行 pytest 和 smoke test、写入*已验证*的测试结果（覆盖 agent 的草稿）
7. **运行结构化预检查**：检测 DummyDataset 使用、检查论文是否有 URL 字段、标记可疑的完美指标等
8. **调度 Critic** 进行对抗性评审 — critic 输出结构化裁定（PASS/REVISE/FAIL）、各维度评分和 `failure_type`
9. **三层门控评估**：critic 裁定 → 预检查覆盖 → 加权分数校验。裁定只能被*降级*，不能被升级
10. **路由决策**：
    - **PASS** → 推进到下一阶段
    - **REVISE**（同阶段故障）→ 带着 critic 反馈重新调度 agent
    - **FAIL**（跨阶段故障）→ 根据 `failure_type` 回退到对应阶段
    - **人类门控**（假设形成和实验阶段）→ 暂停等待人工批准
11. **重复**直到 pipeline 完成或修订次数耗尽

### 核心设计决策

| 决策 | 原因 |
|------|------|
| **Agent 是 CLI 子进程，不是库调用** | 真正的进程级隔离。每个 agent 有独立的上下文、工具集和崩溃边界。Orchestrator 能存活于 agent 崩溃 |
| **通过 YAML 文件通信，不通过消息** | 磁盘上的产物可审计、可版本化、不需要共享内存。添加新 agent 不需要修改通信协议 |
| **Verdict 默认 REVISE，永不默认 PASS** | 如果 critic 的输出无法解析，安全默认是要求修订。缺失的评分计为 0.0。防止质量被绕过 |
| **Orchestrator 独立验证执行** | Agent 不能自报测试通过。Orchestrator 落盘代码并实际运行，然后写入自己的已验证产物 |
| **故障类型驱动回退路由** | 不是泛泛地"重试"，而是 critic 分类*为什么*失败（设计缺陷、假设需修改等），pipeline 路由到对应的早期阶段 |

### 架构

```
Python Orchestrator（scripts/multi_agent.py 或 gui.py）
│
├── [Claude CLI]    →  Researcher Agent  ← agents/researcher/CLAUDE.md
│   默认：claude -p, Claude Opus 4
│   工具：Read, Write, Glob, Grep, WebSearch, WebFetch, Agent
│
├── [OpenCode CLI]  →  Engineer Agent    ← agents/engineer/CLAUDE.md
│   默认：opencode run, Doubao Seed 2.0 Code
│   工具：Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Agent
│
├── [Codex CLI]     →  Critic Agent      ← agents/critic/CLAUDE.md
│   默认：codex exec, GPT-5.4
│   权限：完整项目沙箱（只读）
│
├── Orchestrator 执行（纯 Python，无 LLM 调用）
│   - 从 YAML 中提取代码落盘
│   - 运行 pytest 和 smoke test
│   - 从输出文件和 stdout 解析 metrics
│   - 写入已验证的 test_result 和 metrics 产物
│
└── 人类门控
    需要人工批准的阶段：hypothesis_formation, experimentation
```

**后端灵活性**：每个 agent 的 CLI 后端和模型都可以通过 `config/settings.yaml` 或 Web GUI 独立配置。可以所有 agent 都用 Claude，也可以混合使用 Claude + Codex + OpenCode，甚至每个角色用不同的模型。

### 7 阶段 Pipeline

Pipeline 是一个有 7 个有序阶段的有限状态机。每个阶段有主 agent、所需产物和加权门控标准。

| # | 阶段 | 主 Agent | 审查者 | 所需产物 | 人类门控 |
|---|------|---------|--------|---------|---------|
| 0 | 问题定义 | Researcher | Critic | `problem_brief` | 否 |
| 1 | 文献综述 | Researcher | Critic | `literature_map`, `evidence_table` | 否 |
| 2 | 假设形成 | Researcher | Critic | `hypothesis_card` | **是** |
| 3 | 实验设计 | Engineer | Critic | `experiment_spec` | 否 |
| 4 | 代码实现 | Engineer | Critic | `code`, `test_result` | 否 |
| 5 | 实验执行 | Engineer | Engineer | `run_manifest`, `metrics` | **是** |
| 6 | 结果分析 | Researcher | Critic | `result_report`, `claim_checklist` | 否 |

#### 状态转换

前进需要门控通过。后退（回退）由特定的故障类型触发：

```
前进（需门控通过）：
  问题定义 → 文献综述 → 假设形成 → 实验设计 → 代码实现 → 实验执行 → 结果分析

后退（故障类型驱动）：
  假设形成  ← 文献综述       （需要更多证据）
  实验设计  ← 假设形成       （假设需修改）
  代码实现  ← 实验设计       （发现设计缺陷）
  实验执行  ← 代码实现       （发现代码 bug）
  结果分析  ← 实验执行       （需要更多实验）
  结果分析  → 假设形成       （假设被证伪）
```

#### 修订循环

当门控以同阶段故障类型失败时，agent 会带着 critic 反馈被重新调度，最多重复 `max_iterations`（默认：5）次。每次迭代通过语义版本号追踪：`major.minor`，其中 `major` = 阶段索引（0-6），`minor` = 阶段内迭代次数。

### 三层质量门控

每个阶段必须通过**三层质量门控**才能推进。每层只能*降级*裁定，不能升级。

**第一层：Critic 评审**

Critic agent 对每个阶段按加权标准评分（定义在 `config/stages.yaml` 中）。加权平均分须达到 `pass_threshold`（默认 0.7）且无阻塞性问题。Critic 还会输出 `failure_type`（7 种类型之一）决定 pipeline 是原地修订还是回退。

**第二层：结构化预检查**

在 critic 评审之前自动运行的检查，捕获常见失败模式：

| 阶段 | 检查 | 作用 |
|------|------|------|
| 文献综述 | 论文 URL 字段存在性 | 标记缺少 `url` 字段的论文 |
| 代码实现 | DummyDataset 检测 | 使用虚假数据则自动 REVISE |
| 代码实现 | Orchestrator 独立测试执行 | 独立运行 pytest，写入已验证的 test_result |
| 实验执行 | 指标造假检测 | 如果所有指标都"刚好"超过目标则标记 |
| 结果分析 | Claim checklist 完成度 | 如果 < 50% 的 claim 已验证则 REVISE |

如果预检查发现阻塞性问题而 critic 说了 PASS，裁定会被覆盖为 REVISE。

**第三层：加权分数校验**

如果 critic 说了 PASS 但加权平均分低于阈值，裁定被覆盖为 REVISE。缺失的评分字段计为 0.0 — agent 不能通过省略低分维度来绕过门控。

### Agent 角色

#### Researcher（研究员）

负责所有知识工作：文献分析、缺口识别、假设形成、结果解读。

- **工具**：Web 搜索、论文检索、结构化分析、子 agent 派生
- **约束**：不能运行代码、不能设计实验（使用 Claude 后端时通过 `--allowedTools` 强制执行）
- **引用规则**：每篇论文必须有 `url` 字段。不确定是否真实存在的论文必须标记 `verified: false`
- **产物**：`problem_brief`、`literature_map`、`evidence_table`、`hypothesis_card`、`result_report`、`claim_checklist`

#### Engineer（工程师）

将假设转化为可执行实验。

- **工具**：代码编写、测试、调试、Bash 执行、子 agent 派生
- **约束**：不能修改假设、不能做质量判断
- **代码规则**：必须使用真实数据集（使用 DummyDataset 自动 FAIL）、固定随机种子、包含测试
- **草稿 vs 已验证**：Engineer 产出*草稿*测试结果。Orchestrator 独立落盘代码、运行测试，写入 Critic 审查的*已验证*产物
- **产物**：`experiment_spec`、`code`、`test_result`、`run_manifest`、`metrics`

#### Critic（审稿人）

对抗性审查者，默认持怀疑态度。

- **权限**：完整项目沙箱（只读）
- **约束**：不能写文件、不能创建产物、不能运行命令
- **输出**：结构化 YAML — 裁定（PASS/REVISE/FAIL）、各维度评分（0.0-1.0）、阻塞性问题、用于回退路由的 `failure_type`
- **产物**：评审输出（仅 stdout，不写入文件）

#### Orchestrator（编排器）

Python 代码本身 — 不是 LLM agent。

- 构建包含前序产物上下文和历史反馈的 TaskCard
- 通过配置的 CLI 后端调度 agent
- 验证并注册产出的产物
- 独立落盘代码并运行测试（实现/实验阶段）
- 通过三层系统评估门控裁定
- 路由 pipeline：推进 / 修订 / 回退 / 人工升级
- 追踪版本时间线、成本和状态转换

### 安装与快速开始

#### 前置要求

- Python 3.11+
- 至少安装一个 CLI 后端：
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（`claude --version`）
  - [OpenAI Codex](https://github.com/openai/codex)（`npm install -g @openai/codex && codex login`）— 用于 critic 评审
  - [OpenCode](https://github.com/opencode-ai/opencode)（可选，用于低成本工程任务）

#### 安装

```bash
git clone <repo-url> && cd research-agent
pip install -e .
```

#### 快速开始

```bash
# 1. 创建项目
ra init "我的研究" -q "选择性注意力剪枝能否将 VLA 推理延迟降低 40%？"

# 2. 全自动运行
python scripts/multi_agent.py auto

# 3. 或使用 Web GUI
python scripts/multi_agent.py gui
# 打开 http://127.0.0.1:8080
```

### 运行 Pipeline

#### 全自动模式

```bash
python scripts/multi_agent.py auto                                    # 运行到完成或人类门控
python scripts/multi_agent.py auto --until hypothesis_formation       # 运行到指定阶段
python scripts/multi_agent.py auto -n 5                               # 每阶段最多 5 次修订
```

pipeline 自动运行，仅在人类门控处暂停（hypothesis_formation 和 experimentation），需要 `ra advance --approve` 批准。

#### 逐步模式

```bash
python scripts/multi_agent.py step                    # 运行一次 agent + 一次评审
python scripts/multi_agent.py step -i "重点关注 transformer 方法"
python scripts/multi_agent.py review                  # 仅运行 critic 评审
```

#### 状态管理（`ra` CLI）

```bash
ra status                   # 查看 pipeline 状态和进度
ra advance --approve        # 批准人类门控
ra rollback literature_review --reason "需要更多论文"
ra cost                     # 查看成本明细（按阶段/agent/模型）
ra artifacts                # 列出所有产物
ra history                  # 查看阶段转换历史
ra projects                 # 列出所有项目
ra use <project-id>         # 切换活跃项目
```

### Web GUI

通过 `python scripts/multi_agent.py gui` 启动（默认：http://127.0.0.1:8080）。

GUI 功能：

- **项目侧边栏**：创建、切换、删除项目
- **Pipeline 控制**：Auto、Step、Review、Stop、Approve/Reject 按钮
- **阶段可视化**：带迭代次数和门控裁定的进度条
- **版本时间线**：每次 agent 运行、门控评估、回退和人工决策
- **详情面板**：每个事件的完整输出，含产物查看器
- **设置面板**：运行时修改每个 agent 的 CLI 后端、模型和 effort
- **控制台**：实时日志输出，支持搜索
- **统计面板**：按阶段和 agent 的成本与耗时分析
- **浏览器通知**：需要人工批准或 pipeline 结束时弹出提醒

设置修改在下一个 pipeline 步骤生效，无需重启。

### 配置

所有配置在 `config/settings.yaml` 中：

```yaml
agents:
  researcher:
    backend: claude                           # claude | codex | opencode
    model: claude-opus-4-20250514
    effort: max
    max_turns: 30
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

cost:
  warning_threshold: 5.0    # 美元
  hard_limit: 50.0
```

阶段特定的门控标准和权重定义在 `config/stages.yaml` 中。每个阶段定义：所需产物、加权门控标准（总和为 1.0）、通过阈值（默认 0.7）、主 agent 和审查者、是否需要人类门控。

### 产物与状态

#### 产物通信

Agent 之间**完全通过**版本化的 YAML 文件通信。没有共享内存，agent 之间没有对话。

产物命名规则 `<type>_v<version>.yaml`，版本单调递增。所有产物不可变 — 修订创建新版本，永不覆盖旧版本。

Orchestrator 将前序产物的内容组装到每个 TaskCard 中，因此每个 agent 接收 pipeline 的累积知识，无需看到其他 agent 的原始输出。

#### 状态持久化

每个项目有唯一的真相源：`projects/<id>/state.json`，一个 JSON 序列化的 Pydantic 模型，包含：

- 当前阶段和迭代次数
- 所有产物（仅追加的注册表）
- 所有门控结果（仅追加）
- 阶段转换记录（前进和回退）
- 成本记录（每次 API 调用的成本追踪）
- 版本时间线（语义版本化的事件日志）

**写入安全性**：状态写入使用排他文件锁（`fcntl.flock`）加原子性临时文件 + 重命名。如果进程崩溃，state.json 要么是旧版本要么是新版本，永远不会是部分写入。

### 已知限制

#### Agent 隔离是软性的

Agent 工具限制（`--allowedTools`）仅在 Claude 后端生效。使用 OpenCode 或 Codex 时，agent 可访问所有工具。Sandbox 模块通过文件系统快照对比事后检测违规，但不能阻止违规发生。

#### 无执行沙箱

Orchestrator 在宿主环境中直接运行 agent 编写的代码（pytest、smoke test）。没有 Docker 容器或 VM 隔离。命令白名单（仅允许 `python`、`pytest`、`pip`，禁止 shell 操作符）可以缓解但不能消除风险。

#### URL 验证仅检查结构

文献综述的预检查只验证论文在 YAML 中是否有 `url` 字段 — 不检查该 URL 是否实际可访问。

#### 独立验证仅限特定阶段

只有代码实现和实验执行阶段有 Orchestrator 独立验证。其余阶段（问题定义、文献综述、假设形成、结果分析），agent 的输出即最终产物，由 critic 评审但不独立复现。

#### 状态文件增长

`state.json` 对产物、门控结果、转换记录和时间线事件使用仅追加列表。多次修订的项目中该文件会持续增长。目前没有压缩或归档机制。

#### 单机运行

系统运行在单台机器上。没有分布式调度、作业队列或云执行后端。

### 路线图

近期优先事项：

- [ ] 提取共享 `PipelineEngine`，消除 CLI/GUI 逻辑重复
- [ ] 将 GUI 前端（HTML/CSS/JS）移到独立静态文件
- [ ] 加强 execution.py 中的命令验证
- [ ] 为文献综述 URL 添加 HTTP HEAD 可达性验证
- [ ] 长期项目的 state 压缩机制
- [ ] 基于 Docker 的执行沙箱
- [ ] 合并 CLI 入口（pipeline.py → ra CLI）

研究方向：

- Verdict 单调性属性的形式化验证
- 跨领域 benchmark（超越机器人/VLA）
- 多机 agent 调度 + 作业队列
- 实验追踪集成（MLflow/W&B 脚手架已存在）
