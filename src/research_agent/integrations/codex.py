"""Codex CLI integration — uses codex-plugin-cc or standalone codex exec.

Two operating modes:
1. Interactive (in Claude Code session): /codex:adversarial-review, /codex:review
2. Programmatic (via scripts/CI): codex exec for non-interactive structured review

The Codex integration is superior to raw OpenAI API calls because:
- Codex can read the actual codebase (sandbox file access)
- Codex has built-in adversarial review mode
- Background job support (non-blocking reviews)
- Same auth as ChatGPT (no separate API key management)
- Review gate can auto-block Claude until Codex approves
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class CodexReviewResult:
    """Structured result from a Codex review."""
    verdict: str           # PASS, FAIL, REVISE
    raw_output: str        # Full Codex output
    scores: dict[str, float] = field(default_factory=dict)
    blocking_issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    strongest_objection: str = ""
    what_would_make_it_pass: str = ""
    exit_code: int = 0


def run_codex_exec(
    prompt: str,
    model: str = "gpt-5.4",
    effort: str = "xhigh",
    project_dir: Optional[Path] = None,
    timeout: int = 900,  # 15 min max, matching review gate timeout
    json_output: bool = True,
) -> tuple[str, int]:
    """Run `codex exec` in non-interactive mode.

    Returns (output_text, exit_code).
    """
    cmd = ["codex", "exec"]

    if model:
        cmd.extend(["--model", model])

    if json_output:
        cmd.append("--json")

    cmd.append(prompt)

    result = subprocess.run(
        cmd,
        cwd=project_dir or Path.cwd(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    output = result.stdout
    if json_output and output.strip():
        # codex exec --json outputs JSONL; extract the last assistant message
        lines = output.strip().split("\n")
        messages = []
        for line in lines:
            try:
                event = json.loads(line)
                if event.get("type") == "message" and event.get("role") == "assistant":
                    messages.append(event.get("content", ""))
            except json.JSONDecodeError:
                continue
        output = "\n".join(messages) if messages else result.stdout

    # Also capture stderr for diagnostics
    if result.returncode != 0 and not output.strip():
        output = result.stderr

    return output, result.returncode


def build_review_prompt(
    stage: str,
    artifact_content: str,
    review_criteria: str,
    project_context: str = "",
) -> str:
    """Build the review prompt for codex exec."""
    return f"""\
You are a rigorous scientific reviewer (Critic Agent) in an automated research pipeline.
Your job is to find real flaws, challenge assumptions, and ensure quality.

RULES:
- Score each criterion 0.0-1.0 with justification
- Distinguish blocking issues (must fix) from suggestions (nice to have)
- NEVER rewrite content — only critique and suggest directions
- When rejecting, explain exactly what would make it pass
- Your verdict MUST be one of: PASS, REVISE, or FAIL (not REJECT or other words)
- PASS: all criteria >= 0.7, no blocking issues
- REVISE: fixable issues, re-submit after addressing
- FAIL: fundamental problems requiring major rework
- Output valid YAML wrapped in ```yaml ... ``` fences
- You are reviewing ONLY the latest version of each artifact, not the full history

STAGE: {stage}

{review_criteria}

---

PROJECT CONTEXT:
{project_context}

ARTIFACT TO REVIEW (latest version only):
{artifact_content}

---

Produce your review as YAML in ```yaml ... ``` fences with these fields:
verdict, scores, blocking_issues, suggestions, strongest_objection, what_would_make_it_pass
"""


def parse_codex_review(raw_output: str) -> CodexReviewResult:
    """Parse Codex output into a structured CodexReviewResult."""
    result = CodexReviewResult(verdict="REVISE", raw_output=raw_output)

    # Try to extract YAML block
    yaml_match = re.search(r"```ya?ml\s*\n(.*?)```", raw_output, re.DOTALL)
    if yaml_match:
        try:
            data = yaml.safe_load(yaml_match.group(1))
            if isinstance(data, dict):
                raw_verdict = str(data.get("verdict", "REVISE")).upper()
                # Normalize non-standard verdicts
                if raw_verdict in ("REJECT", "MAJOR_REVISION"):
                    raw_verdict = "FAIL"
                elif raw_verdict in ("MINOR_REVISION", "CONDITIONAL_ACCEPT"):
                    raw_verdict = "REVISE"
                elif raw_verdict in ("ACCEPT",):
                    raw_verdict = "PASS"
                result.verdict = raw_verdict

                # Handle nested score dicts like {score: 0.8, justification: "..."}
                raw_scores = data.get("scores", {})
                result.scores = {}
                for k, v in raw_scores.items():
                    if isinstance(v, (int, float)):
                        result.scores[k] = float(v)
                    elif isinstance(v, dict) and "score" in v:
                        result.scores[k] = float(v["score"])
                result.blocking_issues = [
                    str(i) for i in data.get("blocking_issues", [])
                ]
                result.suggestions = [
                    str(i) for i in data.get("suggestions", [])
                ]
                result.strongest_objection = str(
                    data.get("strongest_objection", "")
                )
                result.what_would_make_it_pass = str(
                    data.get("what_would_make_it_pass", "")
                )
                return result
        except (yaml.YAMLError, ValueError):
            pass

    # Fallback: detect verdict from text
    upper = raw_output.upper()
    if "VERDICT: PASS" in upper or "VERDICT:PASS" in upper:
        result.verdict = "PASS"
    elif "VERDICT: FAIL" in upper or "VERDICT:FAIL" in upper:
        result.verdict = "FAIL"
    else:
        result.verdict = "REVISE"

    return result


def codex_review(
    stage: str,
    artifact_content: str,
    review_criteria: str,
    project_context: str = "",
    model: str = "gpt-5.4",
    effort: str = "xhigh",
    project_dir: Optional[Path] = None,
) -> CodexReviewResult:
    """Run a full Codex review: build prompt → exec → parse result."""
    prompt = build_review_prompt(stage, artifact_content, review_criteria, project_context)
    output, exit_code = run_codex_exec(
        prompt=prompt,
        model=model,
        effort=effort,
        project_dir=project_dir,
        json_output=False,  # plain text is more reliable for review parsing
    )
    result = parse_codex_review(output)
    result.exit_code = exit_code
    return result


# ---------------------------------------------------------------------------
# Convenience: check if Codex CLI is available
# ---------------------------------------------------------------------------

def check_codex_available() -> tuple[bool, str]:
    """Check if codex CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            return True, f"Codex CLI available: {version}"
        return False, f"Codex CLI returned error: {result.stderr}"
    except FileNotFoundError:
        return False, (
            "Codex CLI not found. Install with:\n"
            "  npm install -g @openai/codex\n"
            "Then authenticate:\n"
            "  codex login"
        )
    except subprocess.TimeoutExpired:
        return False, "Codex CLI timed out"
