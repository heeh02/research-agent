# Claude Code + Codex Plugin Integration Guide

## Setup

```bash
# 1. Install Codex CLI
npm install -g @openai/codex

# 2. Authenticate
codex login

# 3. Install plugin in Claude Code session
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

## Codex Plugin Commands Reference

| Command | Use When | Example |
|---------|----------|---------|
| `/codex:adversarial-review` | Research/design stages — challenges assumptions | `/codex:adversarial-review check if hypothesis is truly novel` |
| `/codex:review --wait` | Code stages — reviews implementation | `/codex:review --base main --wait` |
| `/codex:rescue` | Stuck or debugging — delegates to Codex | `/codex:rescue --effort high investigate the OOM error` |
| `/codex:status` | Check background jobs | `/codex:status` |
| `/codex:result` | View completed review | `/codex:result` |
| `/codex:cancel` | Stop active review | `/codex:cancel` |
| `/codex:setup --enable-review-gate` | Auto-block Claude until Codex approves | Critical stages only |

## Pipeline Integration Workflow

```
Claude Code session:

1. python scripts/pipeline.py status         ← Where am I?
2. python scripts/pipeline.py run            ← What to do?
3. [Write artifact YAML files]               ← Do the work
4. python scripts/pipeline.py save <type> <file>  ← Register
5. /codex:adversarial-review                 ← Codex reviews (reads full codebase!)
6. [Address feedback if REVISE]              ← Fix issues
7. python scripts/pipeline.py advance        ← Move forward
```

## Review Gate (Advanced)

The review gate auto-blocks Claude Code output until Codex reviews:

```
/codex:setup --enable-review-gate    # Enable (use for critical stages)
/codex:setup --disable-review-gate   # Disable
```

**When to use**: implementation and experimentation stages where errors are costly.
**When NOT to use**: early research stages where iteration speed matters more.

## Hooks in .claude/settings.json

The project configures these hooks:
- **PreToolUse/Write**: Blocks writing to sensitive files (.env, credentials)
- **PostToolUse/Write**: Auto-validates Python syntax and YAML schemas
- **Notification**: Shows active project status

## Codex Project Config (.codex/config.toml)

The `.codex/config.toml` file tells Codex about the research pipeline context,
so it knows the project structure and review criteria when running reviews.
