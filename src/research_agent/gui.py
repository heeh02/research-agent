"""Web GUI — version timeline + CLI backend selector for multi-agent pipeline.

Launch: python scripts/multi_agent.py gui [--port 8080]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .models import STAGE_ORDER, STAGE_PRIMARY_AGENT, AgentRole, CLIBackend, ProjectState, VersionEventType
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
.refresh-btn, .settings-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text-dim); padding: 3px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; font-family: inherit; }
.refresh-btn:hover, .settings-btn:hover { border-color: var(--blue); color: var(--text); }
.settings-btn.active { background: var(--blue); color: var(--bg); border-color: var(--blue); }

/* Settings panel */
.settings-panel { display: none; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }
.settings-panel.visible { display: block; }
.settings-panel h3 { font-size: 13px; color: var(--text-bright); margin-bottom: 12px; }
.settings-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
.agent-config { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; }
.agent-config h4 { font-size: 12px; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
.agent-config .role-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot-researcher { background: var(--blue); }
.dot-engineer { background: var(--green); }
.dot-critic { background: var(--purple); }
.dot-orchestrator { background: var(--orange); }
.config-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.config-row label { font-size: 11px; color: var(--text-dim); min-width: 55px; }
.config-row select, .config-row input { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 4px; font-size: 11px; font-family: inherit; flex: 1; }
.config-row select:focus, .config-row input:focus { border-color: var(--blue); outline: none; }
.save-btn { background: var(--green); color: var(--bg); border: none; padding: 6px 18px; border-radius: 6px; cursor: pointer; font-size: 12px; font-family: inherit; font-weight: 600; margin-top: 12px; }
.save-btn:hover { opacity: .9; }
.save-msg { font-size: 11px; color: var(--green); margin-left: 12px; display: none; }

/* Stage progress */
.stages { display: flex; gap: 3px; margin-bottom: 16px; }
.stage-chip { flex: 1; padding: 7px 4px; border-radius: 6px; text-align: center; font-size: 10px; background: var(--surface); border: 1px solid var(--border); cursor: pointer; transition: all .15s; }
.stage-chip:hover { border-color: var(--blue); }
.stage-chip.done { background: #0d2818; border-color: var(--green); color: var(--green); }
.stage-chip.active { background: #1a1f35; border-color: var(--blue); color: var(--blue); box-shadow: 0 0 8px rgba(88,166,255,.25); }
.stage-chip.failed { background: #2d1215; border-color: var(--red); color: var(--red); }
.stage-chip .agent-label { display: block; font-size: 8px; color: var(--text-dim); margin-top: 1px; }

/* Main layout */
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

/* Detail panel */
.detail { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; overflow: hidden; }
.detail-header { padding: 14px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0; display: flex; justify-content: space-between; align-items: center; }
.detail-header h2 { font-size: 15px; color: var(--text-bright); }
.detail-scroll { flex: 1; overflow-y: auto; padding: 14px 18px; }
.show-all-btn { background: var(--border); border: 1px solid var(--text-dim); color: var(--text); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 11px; font-family: inherit; white-space: nowrap; }
.show-all-btn:hover { background: var(--blue); color: var(--bg); border-color: var(--blue); }

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

.scores { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.score-chip { font-size: 10px; padding: 1px 7px; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); }
.score-chip.pass { border-color: var(--green); color: var(--green); }
.score-chip.fail { border-color: var(--red); color: var(--red); }

.artifact-list { list-style: none; margin-top: 6px; }
.artifact-list li { font-size: 10px; padding: 2px 0; color: var(--cyan); }
.artifact-list li::before { content: "\1F4C4 "; }

.detail-text { font-size: 11px; white-space: pre-wrap; word-break: break-word; color: var(--text); background: var(--bg); padding: 10px; border-radius: 4px; border: 1px solid var(--border); margin-top: 8px; max-height: 200px; overflow-y: auto; cursor: pointer; transition: max-height .3s ease; }
.detail-text.expanded { max-height: none; }
.detail-text-toggle { font-size: 10px; color: var(--blue); cursor: pointer; margin-top: 4px; user-select: none; }

.error-banner { background: #2d1215; border: 1px solid var(--red); color: var(--red); padding: 10px 16px; border-radius: 6px; margin-bottom: 12px; font-size: 12px; display: none; }

.footer { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); text-align: center; font-size: 10px; color: var(--text-dim); }

@media (max-width: 800px) {
  .main { grid-template-columns: 1fr; height: auto; }
  .sidebar, .detail { max-height: 50vh; }
  .stages { flex-wrap: wrap; }
  .settings-grid { grid-template-columns: 1fr; }
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
      <button class="settings-btn" id="settingsBtn" onclick="toggleSettings()">CLI Settings</button>
      <button class="refresh-btn" onclick="location.reload()">Refresh</button>
    </div>
  </div>

  <!-- CLI Backend Settings Panel -->
  <div class="settings-panel" id="settingsPanel">
    <h3>CLI Backend Configuration</h3>
    <div class="settings-grid" id="settingsGrid"></div>
    <div style="display:flex;align-items:center;margin-top:12px;">
      <button class="save-btn" onclick="saveSettings()">Save &amp; Apply</button>
      <span class="save-msg" id="saveMsg">Saved!</span>
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
      <div class="detail-header">
        <h2 id="detailTitle">Select a version</h2>
        <button class="show-all-btn" id="showAllBtn" onclick="toggleShowAll()">Show All</button>
      </div>
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
const CONFIG = {{CONFIG_JSON}};

document.getElementById('eventCount').textContent = DATA.timeline.length;

// ========================
// Settings Panel
// ========================

const BACKENDS = ['claude', 'codex', 'opencode'];
const MODELS = {
  claude: ['claude-sonnet-4-20250514','claude-opus-4-20250514','claude-haiku-4-5-20251001'],
  codex: ['gpt-5.4','gpt-5.4-mini','gpt-4.1','gpt-4o','o3'],
  opencode: {{OPENCODE_MODELS_JSON}},
};
const EFFORTS = {
  claude: ['max','high','medium','low'],
  codex: ['xhigh','high','medium','low'],
  opencode: ['max','high','medium','low','minimal'],
};
const ROLES = ['researcher','engineer','critic','orchestrator'];
const ROLE_COLORS = {researcher:'blue',engineer:'green',critic:'purple',orchestrator:'orange'};

function buildSettings() {
  const grid = document.getElementById('settingsGrid');
  grid.innerHTML = '';
  ROLES.forEach(role => {
    const cfg = CONFIG.agents?.[role] || {};
    const card = document.createElement('div');
    card.className = 'agent-config';
    card.innerHTML = `
      <h4><span class="role-dot dot-${role}"></span>${role}</h4>
      <div class="config-row">
        <label>CLI</label>
        <select id="cfg-${role}-backend" onchange="onBackendChange('${role}')">
          ${BACKENDS.map(b => `<option value="${b}" ${cfg.backend===b?'selected':''}>${b}</option>`).join('')}
        </select>
      </div>
      <div class="config-row">
        <label>Model</label>
        <select id="cfg-${role}-model"></select>
      </div>
      <div class="config-row">
        <label>Effort</label>
        <select id="cfg-${role}-effort"></select>
      </div>
    `;
    grid.appendChild(card);
    onBackendChange(role, cfg.model, cfg.effort);
  });
}

function onBackendChange(role, currentModel, currentEffort) {
  const backend = document.getElementById(`cfg-${role}-backend`).value;
  const modelSel = document.getElementById(`cfg-${role}-model`);
  const effortSel = document.getElementById(`cfg-${role}-effort`);

  const models = MODELS[backend] || [];
  modelSel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
  if (currentModel && models.includes(currentModel)) modelSel.value = currentModel;

  const efforts = EFFORTS[backend] || ['high'];
  effortSel.innerHTML = efforts.map(e => `<option value="${e}">${e}</option>`).join('');
  if (currentEffort && efforts.includes(currentEffort)) effortSel.value = currentEffort;
}

function toggleSettings() {
  const panel = document.getElementById('settingsPanel');
  const btn = document.getElementById('settingsBtn');
  const visible = panel.classList.toggle('visible');
  btn.classList.toggle('active', visible);
}

function saveSettings() {
  const settings = {};
  ROLES.forEach(role => {
    settings[role] = {
      backend: document.getElementById(`cfg-${role}-backend`).value,
      model: document.getElementById(`cfg-${role}-model`).value,
      effort: document.getElementById(`cfg-${role}-effort`).value,
    };
  });
  fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(settings),
  }).then(r => r.json()).then(d => {
    const msg = document.getElementById('saveMsg');
    msg.style.display = 'inline';
    msg.textContent = d.ok ? 'Saved! Restart pipeline to apply.' : 'Error: ' + d.error;
    setTimeout(() => msg.style.display = 'none', 3000);
  }).catch(e => {
    const msg = document.getElementById('saveMsg');
    msg.style.display = 'inline';
    msg.textContent = 'Network error';
    setTimeout(() => msg.style.display = 'none', 3000);
  });
}

buildSettings();

// ========================
// Stages
// ========================
const stagesEl = document.getElementById('stages');
STAGES.forEach(s => {
  const d = document.createElement('div');
  d.className = 'stage-chip ' + s.status;
  const backend = CONFIG.agents?.[s.agent]?.backend || 'claude';
  d.innerHTML = `v${s.index}.x ${s.name.replace(/_/g,' ')}<span class="agent-label">${s.agent} (${backend})</span>`;
  stagesEl.appendChild(d);
});

// ========================
// Timeline
// ========================
const versions = {};
DATA.timeline.forEach(ev => {
  if (!versions[ev.version]) versions[ev.version] = { events: [], stage: ev.stage };
  versions[ev.version].events.push(ev);
});

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

let allExpanded = false;
function toggleShowAll() {
  allExpanded = !allExpanded;
  document.querySelectorAll('.detail-text').forEach(el => {
    if (allExpanded) el.classList.add('expanded');
    else el.classList.remove('expanded');
  });
  document.querySelectorAll('.detail-text-toggle').forEach(el => {
    el.textContent = allExpanded ? 'Show less' : 'Show more';
  });
  document.getElementById('showAllBtn').textContent = allExpanded ? 'Collapse All' : 'Show All';
}

if (sortedVers.length) {
  const last = sortedVers[sortedVers.length-1];
  const el = document.querySelector(`.version-header[data-ver="${last}"]`);
  if (el) selectVersion(last, el);
}

setInterval(() => {
  fetch('/api/state').then(r => r.json()).then(d => {
    if (d.timeline.length !== DATA.timeline.length) location.reload();
  }).catch(() => {});
}, 10000);
</script>
</body>
</html>"""


def _get_opencode_models() -> list[str]:
    """Get available opencode models by running `opencode models`."""
    import subprocess, os
    opencode_bin = os.environ.get("OPENCODE_BIN", os.path.expanduser("~/.opencode/bin/opencode"))
    try:
        result = subprocess.run(
            [opencode_bin, "models"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except Exception:
        pass
    return ["volcengine-plan/doubao-seed-2.0-pro", "volcengine-plan/deepseek-v3.2"]


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


def render_html(state: ProjectState, config: dict) -> str:
    data = build_gui_data(state)
    opencode_models = _get_opencode_models()
    html = _HTML_TEMPLATE
    html = html.replace("{{PROJECT_NAME}}", data["project_name"])
    html = html.replace("{{CURRENT_VERSION}}", data["current_version"])
    html = html.replace("{{TOTAL_COST}}", data["total_cost"])
    html = html.replace("{{DATA_JSON}}", json.dumps({"timeline": data["timeline"]}))
    html = html.replace("{{STAGES_JSON}}", json.dumps(data["stages"]))
    html = html.replace("{{CONFIG_JSON}}", json.dumps(config))
    html = html.replace("{{OPENCODE_MODELS_JSON}}", json.dumps(opencode_models))
    return html


def run_gui(sm: StateManager, project_id: str, config: dict, port: int = 8080):
    from http.server import HTTPServer, BaseHTTPRequestHandler

    config_path = None
    # Find config file
    for d in [sm.base_dir, Path.cwd()]:
        p = d / "config" / "settings.yaml"
        if p.exists():
            config_path = p
            break

    # Use mutable container so nested functions can update config
    cfg_box = [config]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                state = sm.load_project(project_id)
                if config_path and config_path.exists():
                    cfg_box[0] = yaml.safe_load(config_path.read_text()) or {}
                html = render_html(state, cfg_box[0])
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
            elif self.path == "/api/config":
                if config_path and config_path.exists():
                    cfg_box[0] = yaml.safe_load(config_path.read_text()) or {}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(cfg_box[0]).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    new_settings = json.loads(body)
                    c = cfg_box[0]
                    if "agents" not in c:
                        c["agents"] = {}
                    for role, vals in new_settings.items():
                        if role not in c["agents"]:
                            c["agents"][role] = {}
                        c["agents"][role]["backend"] = vals.get("backend", "claude")
                        c["agents"][role]["model"] = vals.get("model", "")
                        c["agents"][role]["effort"] = vals.get("effort", "high")
                    cfg_box[0] = c

                    if config_path:
                        config_path.write_text(
                            yaml.dump(cfg_box[0], default_flow_style=False, allow_unicode=True, width=120),
                            encoding="utf-8",
                        )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True}).encode())
                except Exception as e:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
            else:
                self.send_error(404)

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

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
