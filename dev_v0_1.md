# Dev Log v0.1 — Architecture Remediation Sprint

Date: 2026-04-07
Author: geminihe + Claude Opus 4.6

## Overview

6 remediation tasks executed in sequence based on the top-conference review
and publication roadmap. Focus: fix claim-code mismatches, eliminate dead
code, centralize duplicated logic, add missing infrastructure (cost tracking,
isolation monitoring, schema validation, concurrency safety).

## Tasks Completed

### Task 2.1: Dead Code Removal
- **Deleted**: `gates.py` (246L), `agents/base.py` (48L), `agents/researcher.py` (137L), `agents/engineer.py` (140L) — 571 lines total
- **Why**: These modules were from an earlier API-based architecture, never imported by runtime path (multi_agent.py, gui.py, dispatcher.py)
- **Risk**: None — grep confirmed zero external references
- **Test delta**: 162 → 162 (no new tests, no regressions)

### Task 2.7: Gate Evaluation Extraction
- **Created**: `src/research_agent/gate_eval.py` (93L) — `evaluate_gate_verdict()` with `GateVerdict` dataclass
- **Modified**: `multi_agent.py` and `gui.py` — replaced ~30 lines of duplicated inline gate logic in each with calls to `evaluate_gate_verdict()`
- **Why**: Gate verdict logic (critic parsing → pre-check override → weighted score override) was duplicated in two files. Centralization enables independent testing and future gate injection experiments.
- **Test delta**: 162 → 176 (+14 new tests in `test_gate_eval.py`)

### Task 2.8: Schema Validation Upgrade
- **Modified**: `artifacts.py:validate_artifact_content()` — added 3 new rule types
- **Upgraded**: 4 schemas (`literature_map`, `hypothesis_card`, `code`, `metrics`)
- **New capabilities**:
  - `min_string_lengths`: rejects placeholder values like `claim: "x"`
  - `list_item_fields`: rejects papers without URLs, code files without content, metrics without current values
  - `cross_field_checks`: kill_criteria count must >= testable_predictions count
- **Risk**: Old artifacts from existing projects may not pass new schemas if re-validated. Does NOT affect already-registered artifacts at runtime.
- **Test delta**: 176 → 197 (+21 new tests)

### Task 2.4: Cost Tracking
- **Modified**: `dispatcher.py` — `_run_claude()` now uses `--output-format json`, extracts `total_cost_usd` and token usage. Added `_parse_claude_json()`, `_estimate_cost_from_text()`. `AgentResult` gained `input_tokens`, `output_tokens`, `cost_source` fields.
- **Modified**: `multi_agent.py` — creates `CostRecord` after each dispatch, appends to `state.cost_records`. Displays cost and token counts in step output.
- **Precision**: Claude CLI = exact (from `total_cost_usd`). Codex/OpenCode = estimated (text-length heuristic, marked `cost_source: "estimated"`).
- **BIGGEST RISK**: `_run_claude` switching from `--output-format text` to `--output-format json` changes the output parsing pipeline. Fallback exists for non-JSON output but needs real dispatch testing.
- **Not done**: gui.py cost recording, review-path cost recording.
- **Test delta**: 197 → 207 (+10 new tests)

### Task 2.2: Isolation / Sandbox Monitoring
- **Created**: `src/research_agent/sandbox.py` (131L) — file-system snapshot + diff based violation detection
- **Design**: Before dispatch, snapshot `projects/<id>/`. After dispatch, diff. Flag files outside role's allowed write patterns.
- **Per-role rules**: Researcher → current stage artifacts only. Engineer → artifacts + experiments/. Critic → review_ files only. Orchestrator → artifacts + logs/.
- **Modified**: `dispatcher.py` — snapshot before/check after in dispatch loop. `models.py` — added `ISOLATION_VIOLATION` event type. `multi_agent.py` — logs violations to console and timeline.
- **Honest limitation**: This is write-path monitoring, NOT hard isolation. Agents still run in the full project directory. Violations are detected and recorded, not prevented.
- **Not done**: gui.py integration, Codex path integration (Codex has its own sandbox).
- **Test delta**: 207 → 229 (+22 new tests)

### Task 2.9: Concurrency Safety
- **Modified**: `state.py` — added `fcntl.flock()` with per-project lock file (`state.json.lock`). `_save_state()` takes `LOCK_EX`, `load_project()` takes `LOCK_SH`.
- **Why**: `dispatch_parallel()` can run multiple agents concurrently; without locking, concurrent `save_project()` calls would silently overwrite each other.
- **Limitation**: fcntl is Unix-only. Advisory lock — won't prevent manual file edits. Read-modify-write race still exists at the application level (last writer wins).
- **Test delta**: 229 → 234 (+5 concurrency tests)

## Final Test Count

| Metric | Before | After |
|--------|--------|-------|
| Total tests | 162 | 234 |
| New tests added | — | 72 |
| Dead code removed | — | 571 lines |
| New modules | — | 3 (gate_eval.py, sandbox.py, test files) |

## Known Gaps (for next sprint)

1. **No end-to-end dispatch test** — all verification is module-level. Need a real `multi_agent.py step` run to validate Claude JSON output parsing in the wild.
2. **gui.py not fully integrated** — cost recording and violation detection only in CLI path, not GUI path.
3. **Review-path cost** — `run_review()` dispatches critic but doesn't write CostRecord.
4. **10 schemas not upgraded** — only 4 of 14 schemas have the new validation rules.
5. **Windows incompatibility** — fcntl import will fail on Windows.
