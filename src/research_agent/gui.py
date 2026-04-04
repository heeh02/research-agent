"""Web GUI — version timeline visualization for multi-agent research pipeline.

Provides:
- Version timeline: see every agent interaction organized by version number
- Stage progress: which stage is active, which passed/failed
- Agent activity: who did what, when, with what result
- Cost tracking: per-agent, per-stage breakdown
- Interactive controls: approve/reject gates, provide feedback

Launch: python scripts/multi_agent.py gui [--port 8080]
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import STAGE_ORDER, STAGE_PRIMARY_AGENT, ProjectState, Stage, VersionEventType
from .state import StateManager

# Inline HTML template — no external files needed
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Agent — {{PROJECT_NAME}}</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-dim: #8b949e; --text-bright: #f0f6fc;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  --purple: #bc8cff; --cyan: #39d2c0; --orange: #f0883e;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; background: var(--bg); color: var(--text); line-height: 1.5; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }

/* Header */
.header { display: flex; justify-content: space-between; align-items: center; padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
.header h1 { font-size: 18px; color: var(--text-bright); }
.header .version-badge { background: var(--blue); color: var(--bg); padding: 4px 12px; border-radius: 12px; font-weight: 600; font-size: 14px; }
.header .cost { color: var(--yellow); font-size: 14px; }

/* Stage progress bar */
.stages { display: flex; gap: 4px; margin-bottom: 24px; }
.stage-chip { flex: 1; padding: 8px 6px; border-radius: 6px; text-align: center; font-size: 11px; background: var(--surface); border: 1px solid var(--border); cursor: pointer; transition: all 0.2s; }
.stage-chip:hover { border-color: var(--blue); }
.stage-chip.done { background: #0d2818; border-color: var(--green); color: var(--green); }
.stage-chip.active { background: #1a1f35; border-color: var(--blue); color: var(--blue); box-shadow: 0 0 8px rgba(88,166,255,0.3); }
.stage-chip.failed { background: #2d1215; border-color: var(--red); color: var(--red); }
.stage-chip .agent-label { display: block; font-size: 9px; color: var(--text-dim); margin-top: 2px; }

/* Main layout */
.main { display: grid; grid-template-columns: 280px 1fr; gap: 20px; }

/* Sidebar: version list */
.sidebar { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow-y: auto; max-height: calc(100vh - 200px); }
.sidebar h3 { padding: 12px 16px; font-size: 13px; color: var(--text-dim); border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--surface); }
.version-group { border-bottom: 1px solid var(--border); }
.version-header { padding: 10px 16px; font-size: 13px; font-weight: 600; color: var(--text-bright); cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
.version-header:hover { background: rgba(88,166,255,0.05); }
.version-header.selected { background: rgba(88,166,255,0.1); border-left: 3px solid var(--blue); }
.version-header .stage-tag { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--border); color: var(--text-dim); }
.version-events { padding: 0 16px 8px; }
.version-event { padding: 4px 0; font-size: 11px; color: var(--text-dim); display: flex; align-items: center; gap: 6px; }
.version-event .icon { width: 16px; text-align: center; }

/* Content: detail panel */
.detail { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; overflow-y: auto; max-height: calc(100vh - 200px); }
.detail h2 { font-size: 16px; margin-bottom: 12px; color: var(--text-bright); }
.detail-section { margin-bottom: 20px; }
.detail-section h4 { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }

/* Event detail card */
.event-card { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-bottom: 8px; }
.event-card .event-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.event-card .agent-badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.agent-researcher { background: #1a2744; color: var(--blue); }
.agent-critic { background: #2a1a2e; color: var(--purple); }
.agent-engineer { background: #1a2e1e; color: var(--green); }
.agent-human { background: #2e2a1a; color: var(--yellow); }
.event-card .time { font-size: 10px; color: var(--text-dim); }
.event-card .verdict { font-weight: 600; }
.verdict-PASS { color: var(--green); }
.verdict-FAIL { color: var(--red); }
.verdict-REVISE { color: var(--yellow); }

/* Scores bar */
.scores { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.score-chip { font-size: 10px; padding: 2px 8px; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); }
.score-chip.pass { border-color: var(--green); color: var(--green); }
.score-chip.fail { border-color: var(--red); color: var(--red); }

/* Artifact list */
.artifact-list { list-style: none; }
.artifact-list li { font-size: 11px; padding: 3px 0; color: var(--cyan); }
.artifact-list li::before { content: "📄 "; }

/* Detail text */
.detail-text { font-size: 12px; white-space: pre-wrap; word-break: break-word; color: var(--text); background: var(--bg); padding: 12px; border-radius: 4px; border: 1px solid var(--border); max-height: 300px; overflow-y: auto; }

/* Cost chart */
.cost-bar { display: flex; height: 24px; border-radius: 4px; overflow: hidden; margin-bottom: 4px; }
.cost-segment { display: flex; align-items: center; justify-content: center; font-size: 10px; min-width: 30px; }
.cost-label { font-size: 11px; color: var(--text-dim); display: flex; justify-content: space-between; }

/* Footer */
.footer { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); text-align: center; font-size: 11px; color: var(--text-dim); }

/* Responsive */
@media (max-width: 800px) { .main { grid-template-columns: 1fr; } .stages { flex-wrap: wrap; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{{PROJECT_NAME}}</h1>
    <div style="display:flex;gap:16px;align-items:center;">
      <span class="cost">${{TOTAL_COST}}</span>
      <span class="version-badge">v{{CURRENT_VERSION}}</span>
    </div>
  </div>
  <div class="stages" id="stages"></div>
  <div class="main">
    <div class="sidebar">
      <h3>Version Timeline</h3>
      <div id="timeline"></div>
    </div>
    <div class="detail" id="detail">
      <h2>Select a version to view details</h2>
      <p style="color:var(--text-dim);font-size:13px;">Click a version on the left to see agent interactions, artifacts, and gate results.</p>
    </div>
  </div>
  <div class="footer">Research Agent — Multi-Agent Pipeline Dashboard</div>
</div>

<script>
const DATA = {{DATA_JSON}};
const STAGES = {{STAGES_JSON}};

// Render stage chips
const stagesEl = document.getElementById('stages');
STAGES.forEach(s => {
  const chip = document.createElement('div');
  chip.className = 'stage-chip ' + s.status;
  chip.innerHTML = `v${s.index}.x ${s.name.replace(/_/g,' ')}<span class="agent-label">${s.agent}</span>`;
  stagesEl.appendChild(chip);
});

// Group events by version
const versions = {};
DATA.timeline.forEach(ev => {
  if (!versions[ev.version]) versions[ev.version] = { events: [], stage: ev.stage };
  versions[ev.version].events.push(ev);
});

// Render timeline sidebar
const timelineEl = document.getElementById('timeline');
Object.keys(versions).sort((a,b) => {
  const [ma,ia] = a.split('.').map(Number);
  const [mb,ib] = b.split('.').map(Number);
  return ma !== mb ? ma - mb : ia - ib;
}).forEach(ver => {
  const group = versions[ver];
  const div = document.createElement('div');
  div.className = 'version-group';

  const icons = { agent_run:'▶', gate_review:'◆', gate_passed:'✓', gate_failed:'✗',
    stage_advance:'⏩', stage_rollback:'↩', human_approve:'👤', human_reject:'✋', human_feedback:'💬' };
  const colors = { agent_run:'var(--blue)', gate_passed:'var(--green)', gate_failed:'var(--red)',
    stage_advance:'var(--cyan)', stage_rollback:'var(--orange)', human_approve:'var(--green)',
    human_reject:'var(--red)', human_feedback:'var(--yellow)' };

  const lastEv = group.events[group.events.length - 1];
  const hasPass = group.events.some(e => e.event_type === 'gate_passed');
  const hasFail = group.events.some(e => e.event_type === 'gate_failed');

  div.innerHTML = `
    <div class="version-header" onclick="selectVersion('${ver}')">
      <span>v${ver} ${hasPass ? '✓' : hasFail ? '✗' : ''}</span>
      <span class="stage-tag">${group.stage.replace(/_/g,' ')}</span>
    </div>
    <div class="version-events">
      ${group.events.map(e => `
        <div class="version-event">
          <span class="icon" style="color:${colors[e.event_type]||'var(--text-dim)'}">${icons[e.event_type]||'·'}</span>
          <span>${e.summary.substring(0,50)}</span>
        </div>
      `).join('')}
    </div>
  `;
  timelineEl.appendChild(div);
});

// Select version → show detail
function selectVersion(ver) {
  document.querySelectorAll('.version-header').forEach(h => h.classList.remove('selected'));
  event.target.closest('.version-header')?.classList.add('selected');

  const events = versions[ver]?.events || [];
  const detail = document.getElementById('detail');

  detail.innerHTML = `<h2>Version ${ver} — ${events[0]?.stage.replace(/_/g,' ') || ''}</h2>`;

  events.forEach(ev => {
    const agentClass = ev.agent ? 'agent-' + ev.agent : '';
    const verdictClass = ev.gate_verdict ? 'verdict-' + ev.gate_verdict : '';

    let scoresHtml = '';
    if (ev.scores && Object.keys(ev.scores).length > 0) {
      scoresHtml = '<div class="scores">' +
        Object.entries(ev.scores).map(([k,v]) =>
          `<span class="score-chip ${v >= 0.7 ? 'pass' : 'fail'}">${k}: ${v}</span>`
        ).join('') + '</div>';
    }

    let artifactsHtml = '';
    const arts = [...(ev.artifacts_produced||[]), ...(ev.artifacts_reviewed||[])];
    if (arts.length > 0) {
      artifactsHtml = '<ul class="artifact-list">' + arts.map(a => `<li>${a.split('/').pop()}</li>`).join('') + '</ul>';
    }

    let detailText = '';
    if (ev.detail) {
      detailText = `<div class="detail-text">${ev.detail.replace(/</g,'&lt;').substring(0,2000)}</div>`;
    }

    const costStr = ev.cost_usd > 0 ? ` · $${ev.cost_usd.toFixed(3)}` : '';
    const durStr = ev.duration_seconds > 0 ? ` · ${ev.duration_seconds.toFixed(1)}s` : '';

    detail.innerHTML += `
      <div class="event-card">
        <div class="event-header">
          <div>
            ${ev.agent ? `<span class="agent-badge ${agentClass}">${ev.agent}</span>` : ''}
            <span style="margin-left:8px;font-size:13px;">${ev.summary}</span>
            ${ev.gate_verdict ? `<span class="verdict ${verdictClass}" style="margin-left:8px;">${ev.gate_verdict}</span>` : ''}
          </div>
          <span class="time">${ev.timestamp.substring(11,19)}${costStr}${durStr}</span>
        </div>
        ${scoresHtml}
        ${artifactsHtml}
        ${detailText}
      </div>
    `;
  });
}

// Auto-select latest version
const allVers = Object.keys(versions);
if (allVers.length > 0) {
  const latest = allVers[allVers.length - 1];
  const headers = document.querySelectorAll('.version-header');
  if (headers.length > 0) {
    headers[headers.length - 1].click();
  }
}
</script>
</body>
</html>"""


def build_gui_data(state: ProjectState) -> dict:
    """Build JSON data for the GUI template."""
    # Stage info
    current_idx = STAGE_ORDER.index(state.current_stage)
    stages = []
    for i, s in enumerate(STAGE_ORDER):
        gates = [g for g in state.gate_results if g.stage == s]
        if i < current_idx:
            status = "done"
        elif i == current_idx:
            if gates and gates[-1].status.value == "failed":
                status = "failed"
            else:
                status = "active"
        else:
            status = ""
        stages.append({
            "index": i,
            "name": s.value,
            "agent": STAGE_PRIMARY_AGENT[s].value,
            "status": status,
        })

    # Timeline events serialized
    timeline = []
    for ev in state.timeline:
        timeline.append({
            "version": ev.version,
            "event_type": ev.event_type.value,
            "agent": ev.agent.value if ev.agent else None,
            "stage": ev.stage.value,
            "summary": ev.summary,
            "detail": ev.detail,
            "artifacts_produced": ev.artifacts_produced,
            "artifacts_reviewed": ev.artifacts_reviewed,
            "gate_verdict": ev.gate_verdict,
            "scores": ev.scores,
            "cost_usd": ev.cost_usd,
            "duration_seconds": ev.duration_seconds,
            "timestamp": ev.timestamp.isoformat(),
        })

    return {
        "project_name": state.name,
        "project_id": state.project_id,
        "current_version": state.current_version(),
        "total_cost": f"{state.total_cost():.4f}",
        "timeline": timeline,
        "stages": stages,
    }


def render_html(state: ProjectState) -> str:
    """Render the full HTML page."""
    data = build_gui_data(state)
    html = _HTML_TEMPLATE
    html = html.replace("{{PROJECT_NAME}}", data["project_name"])
    html = html.replace("{{CURRENT_VERSION}}", data["current_version"])
    html = html.replace("{{TOTAL_COST}}", data["total_cost"])
    html = html.replace("{{DATA_JSON}}", json.dumps({"timeline": data["timeline"]}))
    html = html.replace("{{STAGES_JSON}}", json.dumps(data["stages"]))
    return html


def run_gui(sm: StateManager, project_id: str, config: dict, port: int = 8080):
    """Launch a local web server serving the GUI."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                state = sm.load_project(project_id)
                html = render_html(state)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            elif self.path == "/api/state":
                state = sm.load_project(project_id)
                data = build_gui_data(state)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            else:
                self.send_error(404)

        def log_message(self, format, *args):
            pass  # Suppress request logs

    host = config.get("gui", {}).get("host", "127.0.0.1")
    server = HTTPServer((host, port), Handler)
    print(f"Research Agent GUI: http://{host}:{port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGUI stopped.")
        server.server_close()
