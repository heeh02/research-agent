# Research Agent — Multi-Agent Pipeline

This project is a **multi-agent automated research system** where each role runs as a separate Claude Code / Codex process.

## Architecture

```
Python Orchestrator (scripts/multi_agent.py)
│
├── claude -p (Researcher Agent)     ← agents/researcher/CLAUDE.md
│   Tools: Read, Write, Glob, Grep, WebSearch, WebFetch
│   Produces: problem_brief, literature_map, hypothesis_card, result_report
│
├── claude -p (Engineer Agent)       ← agents/engineer/CLAUDE.md
│   Tools: Read, Write, Edit, Bash, Glob, Grep
│   Produces: experiment_spec, code, run_manifest, tests
│
├── codex exec (Critic Agent)        ← .codex/config.toml
│   Reads full codebase in sandbox
│   Produces: review_report with PASS/FAIL/REVISE verdict
│
└── Human Gate
    Approves at: hypothesis_formation, experimentation
```

Each agent is isolated: the Researcher can't run code, the Engineer can't search the web, the Critic can't rewrite artifacts. This enforces separation of concerns.

## Commands

```bash
# Single-agent mode (you are all roles)
python scripts/pipeline.py status
python scripts/pipeline.py run
python scripts/codex_review.py
python scripts/pipeline.py advance

# Multi-agent mode (each role = separate process)
python scripts/multi_agent.py status        # Pipeline state
python scripts/multi_agent.py step          # Run one step (dispatches to right agent)
python scripts/multi_agent.py review        # Run Codex critic
python scripts/multi_agent.py auto          # Full automated run
python scripts/multi_agent.py auto --until hypothesis_formation
```

## Multi-Agent Workflow

```
multi_agent.py auto
│
│  Stage: problem_definition
│  ├── Dispatch → Researcher (claude -p, 15 turns max)
│  │   Reads: task card with research question
│  │   Writes: problem_brief_v1.yaml
│  ├── Dispatch → Codex Critic (codex exec)
│  │   Reads: problem_brief + project context
│  │   Writes: review_report.yaml (PASS/FAIL/REVISE)
│  ├── If REVISE: re-dispatch Researcher with feedback (up to 3x)
│  └── If PASS: advance to literature_review
│
│  Stage: literature_review
│  ├── Dispatch → Researcher (reads problem_brief as context)
│  ...
│
│  Stage: hypothesis_formation
│  ├── Dispatch → Researcher
│  ├── Dispatch → Codex Critic
│  └── HUMAN GATE: waits for approval
│
│  Stage: experiment_design
│  ├── Dispatch → Engineer (reads hypothesis_card)
│  ...
│
│  Stage: implementation
│  ├── Dispatch → Engineer (writes code, runs tests)
│  ├── Dispatch → Codex Critic (reviews code)
│  ...
```

## Rules

1. **ALWAYS check status first**: `python scripts/multi_agent.py status`
2. **Agents communicate via artifacts, not conversation** — all state in files
3. **Codex critic reviews every stage** before advancing
4. **Human gates** at hypothesis_formation and experimentation
5. **Max 3 revision cycles** per stage before escalating to human

## When to Use Which Mode

| Scenario | Mode | Command |
|----------|------|---------|
| Quick exploration | Single-agent | `python scripts/pipeline.py run` |
| Full research project | Multi-agent auto | `python scripts/multi_agent.py auto` |
| Step-by-step control | Multi-agent step | `python scripts/multi_agent.py step` |
| Interactive Codex review | Plugin | `/codex:adversarial-review` |
| CI/CD gate | Script | `python scripts/codex_review.py` |

## Setup

```bash
# 1. Install
pip install -e .

# 2. Codex CLI
npm install -g @openai/codex && codex login

# 3. Verify Claude Code CLI
claude --version

# 4. Create project
python scripts/pipeline.py init "My Research" -q "Research question?"

# 5. Run
python scripts/multi_agent.py auto
```

## Directory Structure

```
agents/
├── researcher/CLAUDE.md     # Researcher role instructions
├── engineer/CLAUDE.md       # Engineer role instructions
└── orchestrator/CLAUDE.md   # Orchestrator role instructions
.codex/config.toml           # Codex critic config
config/settings.yaml         # Agent models, tools, pipeline settings
projects/<id>/
├── state.json               # Pipeline state
├── artifacts/<stage>/       # YAML artifacts per stage
├── experiments/             # Experiment code
└── logs/                    # Execution logs
scripts/
├── multi_agent.py           # Multi-agent orchestrator
├── pipeline.py              # State management CLI
├── codex_review.py          # Codex review (programmatic)
└── setup.sh                 # One-click setup
```
