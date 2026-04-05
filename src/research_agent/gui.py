"""Web GUI — version timeline visualization for multi-agent research pipeline.

Launch: python scripts/multi_agent.py gui [--port 8080]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import STAGE_ORDER, STAGE_PRIMARY_AGENT, ProjectState, Stage, VersionEventType
from .state import StateManager

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
body { font-family: 'SF Mono','Cascadia Code','Consolas',monospace; background: var(--bg); color: var(--text); line-height: 1.6; }
.container { max-width: 1400px; margin: 0 auto; padding: 16px 20px; }

/* Header */
.header { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; flex-wrap: wrap; gap: 8px; }
.header h1 { font-size: 17px; color: var(--text-bright); }
.header-right { display: flex; gap: 14px; align-items: center; }
.version-badge { background: var(--blue); color: var(--bg); padding: 3px 12px; border-radius: 12px; font-weight: 600; font-size: 13px; }
.cost { color: var(--yellow); font-size: 13px; }
.refresh-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text-dim); padding: 3px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; font-family: inherit; }
.refresh-btn:hover { border-color: var(--blue); color: var(--text); }

/* Stage progress */
.stages { display: flex; gap: 3px; margin-bottom: 16px; }
.stage-chip { flex: 1; padding: 7px 4px; border-radius: 6px; text-align: center; font-size: 10px; background: var(--surface); border: 1px solid var(--border); cursor: pointer; transition: all .15s; }
.stage-chip:hover { border-color: var(--blue); }
.stage-chip.done { background: #0d2818; border-color: var(--green); color: var(--green); }
.stage-chip.active { background: #1a1f35; border-color: var(--blue); color: var(--blue); box-shadow: 0 0 8px rgba(88,166,255,.25); }
.stage-chip.failed { background: #2d1215; border-color: var(--red); color: var(--red); }
.stage-chip .agent-label { display: block; font-size: 8px; color: var(--text-dim); margin-top: 1px; }

/* Main layout — full height */
.main { display: grid; grid-template-columns: 260px 1fr; gap: 16px; height: calc(100vh - 160px); min-height: 400px; }

/* Sidebar */
.sidebar { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; overflow: hidden; }
.sidebar h3 { padding: 10px 14px; font-size: 12px; color: var(--text-dim); border-bottom: 1px solid var(--border); flex-shrink: 0; }
.sidebar-scroll { flex: 1; overflow-y: auto; }
.version-group { border-bottom: 1px solid var(--border); }
.version-header { padding: 9px 14px; font-size: 12px; font-weight: 600; color: var(--text-bright); cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
.version-header:hover { background: rgba(88,166,255,.05); }
.version-header.selected { background: rgba(88,166,255,.1); border-left: 3px solid var(--blue); }
.stage-tag { font-size: 9px; padding: 1px 6px; border-radius: 4px; background: var(--border); color: var(--text-dim); white-space: nowrap; }
.version-events { padding: 0 14px 6px; }
.version-event { padding: 3px 0; font-size: 10px; color: var(--text-dim); display: flex; align-items: center; gap: 5px; }
.version-event .icon { width: 14px; text-align: center; flex-shrink: 0; }

/* Detail panel — FULLY SCROLLABLE */
.detail { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; overflow: hidden; }
.detail-header { padding: 14px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.detail-header h2 { font-size: 15px; color: var(--text-bright); }
.detail-scroll { flex: 1; overflow-y: auto; padding: 14px 18px; }

/* Event card */
.event-card { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-bottom: 10px; }
.event-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px; flex-wrap: wrap; gap: 4px; }
.agent-badge { font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 600; white-space: nowrap; }
.agent-researcher { background: #1a2744; color: var(--blue); }
.agent-critic { background: #2a1a2e; color: var(--purple); }
.agent-engineer { background: #1a2e1e; color: var(--green); }
.agent-human { background: #2e2a1a; color: var(--yellow); }
.ev-summary { font-size: 12px; flex: 1; min-width: 0; }
.ev-meta { font-size: 10px; color: var(--text-dim); white-space: nowrap; }
.verdict { font-weight: 600; }
.verdict-PASS { color: var(--green); }
.verdict-FAIL { color: var(--red); }
.verdict-REVISE { color: var(--yellow); }

/* Scores */
.scores { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.score-chip { font-size: 10px; padding: 1px 7px; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); }
.score-chip.pass { border-color: var(--green); color: var(--green); }
.score-chip.fail { border-color: var(--red); color: var(--red); }

/* Artifact list */
.artifact-list { list-style: none; margin-top: 6px; }
.artifact-list li { font-size: 10px; padding: 2px 0; color: var(--cyan); }
.artifact-list li::before { content: "\1F4C4 "; }

/* Detail text — EXPANDABLE, no hard max-height */
.detail-text { font-size: 11px; white-space: pre-wrap; word-break: break-word; color: var(--text); background: var(--bg); padding: 10px; border-radius: 4px; border: 1px solid var(--border); margin-top: 8px; max-height: 200px; overflow-y: auto; cursor: pointer; transition: max-height .3s ease; }
.detail-text.expanded { max-height: none; }
.detail-text-toggle { font-size: 10px; color: var(--blue); cursor: pointer; margin-top: 4px; user-select: none; }

/* Error banner */
.error-banner { background: #2d1215; border: 1px solid var(--red); color: var(--red); padding: 10px 16px; border-radius: 6px; margin-bottom: 12px; font-size: 12px; display: none; }

.footer { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); text-align: center; font-size: 10px; color: var(--text-dim); }

@media (max-width: 800px) {
  .main { grid-template-columns: 1fr; height: auto; }
  .sidebar, .detail { max-height: 50vh; }
  .stages { flex-wrap: wrap; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{{PROJECT_NAME}}</h1>
    <div class="header-right">
      <span class="cost">${{TOTAL_COST}}</span>
      <span class="version-badge">v{{CURRENT_VERSION}}</span>
      <button class="refresh-btn" onclick="location.reload()">Refresh</button>
    </div>
  </div>
  <div class="error-banner" id="errorBanner"></div>
  <div class="stages" id="stages"></div>
  <div class="main">
    <div class="sidebar">
      <h3>Version Timeline (<span id="eventCount">0</span> events)</h3>
      <div class="sidebar-scroll" id="timeline"></div>
    </div>
    <div class="detail">
      <div class="detail-header"><h2 id="detailTitle">Select a version</h2></div>
      <div class="detail-scroll" id="detailScroll">
        <p style="color:var(--text-dim);font-size:12px;">Click a version on the left to see agent interactions.</p>
      </div>
    </div>
  </div>
  <div class="footer">Research Agent — Multi-Agent Pipeline</div>
</div>

<script>
const DATA = {{DATA_JSON}};
const STAGES = {{STAGES_JSON}};

document.getElementById('eventCount').textContent = DATA.timeline.length;

// Stages
const stagesEl = document.getElementById('stages');
STAGES.forEach(s => {
  const d = document.createElement('div');
  d.className = 'stage-chip ' + s.status;
  d.innerHTML = `v${s.index}.x ${s.name.replace(/_/g,' ')}<span class="agent-label">${s.agent}</span>`;
  stagesEl.appendChild(d);
});

// Group by version
const versions = {};
DATA.timeline.forEach(ev => {
  if (!versions[ev.version]) versions[ev.version] = { events: [], stage: ev.stage };
  versions[ev.version].events.push(ev);
});

// Sidebar
const timelineEl = document.getElementById('timeline');
const sortedVers = Object.keys(versions).sort((a,b) => {
  const [ma,ia] = a.split('.').map(Number);
  const [mb,ib] = b.split('.').map(Number);
  return ma !== mb ? ma - mb : ia - ib;
});

const icons = { agent_run:'\u25B6', gate_review:'\u25C6', gate_passed:'\u2713', gate_failed:'\u2717',
  stage_advance:'\u23E9', stage_rollback:'\u21A9', human_approve:'\uD83D\uDC64', human_reject:'\u270B', human_feedback:'\uD83D\uDCAC' };
const colors = { agent_run:'var(--blue)', gate_passed:'var(--green)', gate_failed:'var(--red)',
  stage_advance:'var(--cyan)', stage_rollback:'var(--orange)', human_approve:'var(--green)',
  human_reject:'var(--red)', human_feedback:'var(--yellow)' };

sortedVers.forEach(ver => {
  const g = versions[ver];
  const hasPass = g.events.some(e => e.event_type === 'gate_passed');
  const hasFail = g.events.some(e => e.event_type === 'gate_failed');
  const div = document.createElement('div');
  div.className = 'version-group';
  div.innerHTML = `
    <div class="version-header" data-ver="${ver}" onclick="selectVersion('${ver}',this)">
      <span>v${ver} ${hasPass?'\u2713':hasFail?'\u2717':''}</span>
      <span class="stage-tag">${g.stage.replace(/_/g,' ')}</span>
    </div>
    <div class="version-events">${g.events.map(e => `
      <div class="version-event">
        <span class="icon" style="color:${colors[e.event_type]||'var(--text-dim)'}">${icons[e.event_type]||'\u00B7'}</span>
        <span>${e.summary.substring(0,48)}</span>
      </div>`).join('')}
    </div>`;
  timelineEl.appendChild(div);
});

let detailToggleId = 0;

function selectVersion(ver, el) {
  document.querySelectorAll('.version-header').forEach(h => h.classList.remove('selected'));
  if (el) el.classList.add('selected');

  const events = versions[ver]?.events || [];
  document.getElementById('detailTitle').textContent = `Version ${ver} \u2014 ${(events[0]?.stage||'').replace(/_/g,' ')}`;
  const scroll = document.getElementById('detailScroll');
  scroll.innerHTML = '';

  events.forEach(ev => {
    const card = document.createElement('div');
    card.className = 'event-card';

    const agentCls = ev.agent ? 'agent-'+ev.agent : '';
    const verdictCls = ev.gate_verdict ? 'verdict-'+ev.gate_verdict : '';
    const costStr = ev.cost_usd > 0 ? ` \u00B7 $${ev.cost_usd.toFixed(3)}` : '';
    const durStr = ev.duration_seconds > 0 ? ` \u00B7 ${ev.duration_seconds.toFixed(1)}s` : '';

    let html = `<div class="event-header">
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
        ${ev.agent ? `<span class="agent-badge ${agentCls}">${ev.agent}</span>` : ''}
        <span class="ev-summary">${ev.summary}</span>
        ${ev.gate_verdict ? `<span class="verdict ${verdictCls}">${ev.gate_verdict}</span>` : ''}
      </div>
      <span class="ev-meta">${ev.timestamp.substring(11,19)}${costStr}${durStr}</span>
    </div>`;

    if (ev.scores && Object.keys(ev.scores).length) {
      html += '<div class="scores">' + Object.entries(ev.scores).map(([k,v]) =>
        `<span class="score-chip ${v>=0.7?'pass':'fail'}">${k}: ${v}</span>`).join('') + '</div>';
    }

    const arts = [...(ev.artifacts_produced||[]),...(ev.artifacts_reviewed||[])];
    if (arts.length) {
      html += '<ul class="artifact-list">' + arts.map(a => `<li>${a.split('/').pop()}</li>`).join('') + '</ul>';
    }

    if (ev.detail) {
      const tid = 'dt' + (++detailToggleId);
      const escaped = ev.detail.replace(/&/g,'&amp;').replace(/</g,'&lt;');
      const isLong = ev.detail.length > 500;
      html += `<div class="detail-text${isLong?'':' expanded'}" id="${tid}" onclick="this.classList.toggle('expanded')">${escaped}</div>`;
      if (isLong) html += `<div class="detail-text-toggle" onclick="document.getElementById('${tid}').classList.toggle('expanded');this.textContent=this.textContent==='Show more'?'Show less':'Show more'">Show more</div>`;
    }

    card.innerHTML = html;
    scroll.appendChild(card);
  });
}

// Auto-select latest
if (sortedVers.length) {
  const last = sortedVers[sortedVers.length-1];
  const el = document.querySelector(`.version-header[data-ver="${last}"]`);
  if (el) selectVersion(last, el);
}

// Auto-refresh every 10s
setInterval(() => {
  fetch('/api/state').then(r => r.json()).then(d => {
    if (d.timeline.length !== DATA.timeline.length) location.reload();
  }).catch(() => {});
}, 10000);
</script>
</body>
</html>"""


def build_gui_data(state: ProjectState) -> dict:
    current_idx = STAGE_ORDER.index(state.current_stage)
    stages = []
    for i, s in enumerate(STAGE_ORDER):
        gates = [g for g in state.gate_results if g.stage == s]
        if i < current_idx:
            status = "done"
        elif i == current_idx:
            status = "failed" if gates and gates[-1].status.value == "failed" else "active"
        else:
            status = ""
        stages.append({"index": i, "name": s.value, "agent": STAGE_PRIMARY_AGENT[s].value, "status": status})

    timeline = []
    for ev in state.timeline:
        timeline.append({
            "version": ev.version, "event_type": ev.event_type.value,
            "agent": ev.agent.value if ev.agent else None, "stage": ev.stage.value,
            "summary": ev.summary, "detail": ev.detail,
            "artifacts_produced": ev.artifacts_produced, "artifacts_reviewed": ev.artifacts_reviewed,
            "gate_verdict": ev.gate_verdict, "scores": ev.scores,
            "cost_usd": ev.cost_usd, "duration_seconds": ev.duration_seconds,
            "timestamp": ev.timestamp.isoformat(),
        })

    return {
        "project_name": state.name, "project_id": state.project_id,
        "current_version": state.current_version(),
        "total_cost": f"{state.total_cost():.4f}",
        "timeline": timeline, "stages": stages,
    }


def render_html(state: ProjectState) -> str:
    data = build_gui_data(state)
    html = _HTML_TEMPLATE
    html = html.replace("{{PROJECT_NAME}}", data["project_name"])
    html = html.replace("{{CURRENT_VERSION}}", data["current_version"])
    html = html.replace("{{TOTAL_COST}}", data["total_cost"])
    html = html.replace("{{DATA_JSON}}", json.dumps({"timeline": data["timeline"]}))
    html = html.replace("{{STAGES_JSON}}", json.dumps(data["stages"]))
    return html


def run_gui(sm: StateManager, project_id: str, config: dict, port: int = 8080):
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
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
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            pass

    host = config.get("gui", {}).get("host", "127.0.0.1")
    server = HTTPServer((host, port), Handler)
    print(f"Research Agent GUI: http://{host}:{port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGUI stopped.")
        server.server_close()
