#!/usr/bin/env python3
"""
Claude Code Live — real-time web viewer for session transcripts.

Usage:
    python3 claude_live.py [session.jsonl] [--port PORT]  (or: claude-code-live)

If no path given, watches the most recently modified session in
~/.claude/projects/
"""

import json
import sys
import re
import argparse
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


# ── Session Discovery ──────────────────────────────────────

def find_latest_session():
    """Find the most recently modified .jsonl session file.
    Skips subagent files — only returns top-level sessions."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    jsonls = [p for p in base.rglob("*.jsonl") if "/subagents/" not in str(p)]
    if not jsonls:
        return None
    return max(jsonls, key=lambda p: p.stat().st_mtime)


def _find_subagent_dir(session_path):
    """Derive the subagents directory for a session file."""
    p = Path(session_path)
    # Session: /.../<uuid>.jsonl → subagents: /.../<uuid>/subagents/
    subdir = p.parent / p.stem / "subagents"
    return subdir if subdir.is_dir() else None


def _parse_subagent_file(path):
    """Parse a full subagent JSONL file into entries."""
    entries = []
    with open(path, "r") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entry = _parse_entry(obj)
            if entry:
                entries.append(entry)
    return entries


# ── JSONL Parsing ──────────────────────────────────────────

def parse_session(path, after_line=0):
    """Parse JSONL entries after a given line offset.
    Returns (entries, new_cursor)."""
    subagent_dir = _find_subagent_dir(path)
    entries = []
    line_num = 0
    with open(path, "r") as f:
        for raw in f:
            line_num += 1
            if line_num <= after_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entry = _parse_entry(obj)
            if entry:
                entries.append(entry)
                # If this entry contains a Task result with an agentId,
                # inline the subagent's entries
                if subagent_dir and entry.get("blocks"):
                    for block in entry["blocks"]:
                        if block.get("type") == "tool_result":
                            agent_id = _extract_agent_id(block.get("content", ""))
                            if agent_id:
                                agent_file = subagent_dir / f"agent-{agent_id}.jsonl"
                                if agent_file.exists():
                                    sub_entries = _parse_subagent_file(agent_file)
                                    if sub_entries:
                                        entries.append({
                                            "type": "subagent",
                                            "agent_id": agent_id,
                                            "timestamp": entry.get("timestamp", ""),
                                            "entries": sub_entries,
                                        })
    return entries, line_num


def _extract_agent_id(text):
    """Extract agentId from a Task tool result text."""
    if not isinstance(text, str):
        return None
    m = re.search(r"agentId:\s*([a-f0-9]+)", text)
    return m.group(1) if m else None


def _parse_entry(obj):
    etype = obj.get("type", "")
    msg = obj.get("message", {})
    role = msg.get("role", "")
    content = msg.get("content", "")
    timestamp = obj.get("timestamp", "")

    if etype in ("system", "file-history-snapshot"):
        return None

    entry = {"timestamp": timestamp, "role": role or etype}

    # Plain text user message
    if role == "user" and isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        if "<command-message>" in text:
            cmd = re.search(r"<command-name>(/\w+)</command-name>", text)
            text = cmd.group(1) if cmd else text
        return {**entry, "type": "user_message", "text": text}

    if not isinstance(content, list):
        return None

    blocks = []
    for block in content:
        parsed = _parse_block(block)
        if parsed:
            blocks.append(parsed)

    if not blocks:
        return None

    if role == "assistant":
        entry["type"] = "assistant"
    elif role == "user":
        # If every block is a tool_result, this is a tool response, not
        # a real user message. Label it differently so the UI doesn't
        # show "USER" for system-generated tool responses.
        all_results = all(b.get("type") == "tool_result" for b in blocks)
        entry["type"] = "tool_response" if all_results else "user_content"
    else:
        entry["type"] = "user_content"
    entry["blocks"] = blocks
    return entry


def _parse_block(block):
    btype = block.get("type", "")

    if btype == "text":
        text = block.get("text", "").strip()
        return {"type": "text", "text": text} if text else None

    if btype == "thinking":
        text = block.get("thinking", "").strip()
        return {"type": "thinking", "text": text} if text else None

    if btype == "tool_use":
        return _parse_tool_use(block)

    if btype == "tool_result":
        return _parse_tool_result(block)

    return None


def _parse_tool_use(block):
    name = block.get("name", "unknown")
    inp = block.get("input", {})
    tb = {"type": "tool_use", "tool": name, "id": block.get("id", "")}

    if name == "Bash":
        tb["command"] = inp.get("command", "")
        tb["description"] = inp.get("description", "")
    elif name == "Read":
        tb["file_path"] = inp.get("file_path", "")
    elif name == "Write":
        tb["file_path"] = inp.get("file_path", "")
        tb["content_length"] = len(inp.get("content", ""))
    elif name == "Edit":
        tb["file_path"] = inp.get("file_path", "")
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        tb["old_string"] = old[:300] + ("…" if len(old) > 300 else "")
        tb["new_string"] = new[:300] + ("…" if len(new) > 300 else "")
    elif name == "Glob":
        tb["pattern"] = inp.get("pattern", "")
    elif name == "Grep":
        tb["pattern"] = inp.get("pattern", "")
        tb["path"] = inp.get("path", "")
    elif name == "WebFetch":
        tb["url"] = inp.get("url", "")
        tb["prompt"] = inp.get("prompt", "")[:200]
    elif name == "WebSearch":
        tb["query"] = inp.get("query", "")
    elif name == "Task":
        tb["description"] = inp.get("description", "")
        tb["agent_type"] = inp.get("subagent_type", "")
    else:
        tb["input_keys"] = list(inp.keys())

    return tb


def _parse_tool_result(block):
    content = block.get("content", "")
    if isinstance(content, str):
        text = content[:3000] + (f"\n… ({len(content)} chars)" if len(content) > 3000 else "")
    elif isinstance(content, list):
        parts = []
        for rc in content:
            if rc.get("type") == "text":
                t = rc.get("text", "")
                parts.append(t[:3000] + (f"\n… ({len(t)} chars)" if len(t) > 3000 else ""))
            elif rc.get("type") == "image":
                parts.append("[image]")
            else:
                parts.append(f"[{rc.get('type', '?')}]")
        text = "\n".join(parts)
    else:
        text = str(content)[:1000]

    return {
        "type": "tool_result",
        "tool_use_id": block.get("tool_use_id", ""),
        "is_error": block.get("is_error", False),
        "content": text,
    }


# ── HTTP Server ────────────────────────────────────────────

class LiveHandler(SimpleHTTPRequestHandler):
    session_path = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._respond(200, "text/html", HTML)
        elif parsed.path == "/api/entries":
            qs = parse_qs(parsed.query)
            after = int(qs.get("after", ["0"])[0])
            entries, cursor = parse_session(self.session_path, after)
            self._respond(200, "application/json", json.dumps({
                "entries": entries, "cursor": cursor
            }))
        elif parsed.path == "/api/info":
            p = Path(self.session_path)
            self._respond(200, "application/json", json.dumps({
                "file": p.name,
                "size": p.stat().st_size,
            }))
        else:
            self._respond(404, "text/plain", "Not found")

    def _respond(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)


# ── Embedded UI ────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Live</title>
<style>
  *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
  :root {
    --bg: #0f1117;
    --bg-card: #181a24;
    --bg-tool: #1c1f30;
    --bg-result: #161924;
    --bg-thinking: #1c1a28;
    --accent: #7c5cfc;
    --accent-dim: rgba(124,92,252,0.15);
    --user: #38bdf8;
    --success: #34d399;
    --danger: #f43f5e;
    --warning: #fbbf24;
    --text: #e2e8f0;
    --text-muted: #b8c4d4;
    --text-dim: #8494a7;
    --text-code: #c8d5e4;
    --border: rgba(255,255,255,0.06);
    --mono: 'JetBrains Mono','Fira Code','Consolas', monospace;
    --sans: 'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    --font-size: 15px;
    --code-size: 13.5px;
  }
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  html { font-size: var(--font-size); }
  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }

  /* ── Header ────────────────────────────────── */
  .header {
    position: sticky; top: 0; z-index: 50;
    background: rgba(15,17,23,0.92);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 0.5rem 1.25rem;
    display: flex; align-items: center; gap: 0.75rem;
    flex-wrap: wrap;
  }
  .header h1 {
    font-size: 1rem; font-weight: 700;
    background: linear-gradient(135deg, var(--accent), var(--user));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    white-space: nowrap;
  }
  .status {
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--success);
    display: flex; align-items: center; gap: 0.35rem;
  }
  .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--success);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  .controls {
    display: flex; gap: 0.6rem; align-items: center; margin-left: auto;
  }
  .controls label {
    font-size: 0.72rem; color: var(--text-dim); cursor: pointer;
    display: flex; align-items: center; gap: 0.25rem;
    user-select: none;
  }
  .controls input[type="checkbox"] { accent-color: var(--accent); }

  .settings-btn {
    background: none; border: 1px solid var(--border);
    color: var(--text-dim); font-size: 0.75rem;
    padding: 0.2em 0.5em; border-radius: 4px;
    cursor: pointer; font-family: var(--mono);
  }
  .settings-btn:hover { border-color: var(--accent); color: var(--accent); }

  .stats {
    font-family: var(--mono); font-size: 0.65rem;
    color: var(--text-dim);
    display: flex; gap: 0.75rem;
  }

  /* ── Settings Panel ────────────────────────── */
  .settings-panel {
    display: none;
    position: fixed; top: 3rem; right: 1rem;
    background: #1a1d2b;
    border: 1px solid rgba(124,92,252,0.2);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    z-index: 100;
    min-width: 280px;
    box-shadow: 0 8px 30px rgba(0,0,0,0.5);
  }
  .settings-panel.open { display: block; }
  .settings-panel h3 {
    font-size: 0.8rem; font-weight: 600; color: var(--accent);
    margin-bottom: 0.75rem;
  }
  .setting-row {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.6rem;
  }
  .setting-row label {
    font-size: 0.8rem; color: var(--text-muted);
  }
  .setting-row input[type="range"] {
    width: 110px;
    accent-color: var(--accent);
  }
  .setting-row .val {
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--text-dim); min-width: 3.5em; text-align: right;
  }
  .setting-row input[type="color"] {
    width: 28px; height: 28px; border: none;
    border-radius: 4px; cursor: pointer;
    background: none; padding: 0;
  }
  .preset-row {
    display: flex; gap: 0.4rem; margin-top: 0.5rem;
  }
  .preset-btn {
    font-family: var(--mono); font-size: 0.65rem;
    background: var(--accent-dim); color: var(--accent);
    border: 1px solid rgba(124,92,252,0.2);
    border-radius: 4px; padding: 0.2em 0.5em;
    cursor: pointer;
  }
  .preset-btn:hover { border-color: var(--accent); }

  /* ── Timeline ──────────────────────────────── */
  #timeline {
    max-width: 960px;
    margin: 0 auto;
    padding: 0.75rem 1.25rem 5rem;
  }

  .entry {
    margin-bottom: 0.4rem;
    animation: fadeIn 0.25s ease;
  }
  @keyframes fadeIn {
    from { opacity:0; transform:translateY(6px); }
    to { opacity:1; transform:translateY(0); }
  }

  /* ── User ──────────────────────────────────── */
  .entry-user {
    background: rgba(56,189,248,0.05);
    border-left: 3px solid var(--user);
    border-radius: 0 8px 8px 0;
    padding: 0.6rem 0.9rem;
  }
  .role-label {
    font-family: var(--mono); font-size: 0.65rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.1em;
    margin-bottom: 0.25rem;
    display: flex; justify-content: space-between;
  }
  .entry-user .role-label { color: var(--user); }
  .msg-text { white-space: pre-wrap; word-break: break-word; }

  /* ── Assistant ─────────────────────────────── */
  .entry-assistant {
    background: var(--bg-card);
    border-left: 3px solid var(--accent);
    border-radius: 0 8px 8px 0;
    padding: 0.6rem 0.9rem;
  }
  .entry-assistant .role-label { color: var(--accent); }

  /* ── Text ──────────────────────────────────── */
  .block-text {
    white-space: pre-wrap; word-break: break-word;
    margin: 0.2rem 0;
  }

  /* ── Thinking ──────────────────────────────── */
  .block-thinking {
    background: var(--bg-thinking);
    border: 1px solid rgba(124,92,252,0.1);
    border-radius: 6px;
    padding: 0.5rem 0.7rem;
    margin: 0.3rem 0;
    color: var(--text-muted);
  }
  .block-thinking summary {
    font-family: var(--mono); font-size: 0.7rem; font-weight: 500;
    color: var(--accent); letter-spacing: 0.05em;
    list-style: none;
    display: flex; align-items: center; gap: 0.35rem;
    cursor: pointer;
  }
  .block-thinking summary::before { content:'▸'; transition:transform 0.2s; }
  .block-thinking[open] summary::before { transform:rotate(90deg); }
  .thought-content {
    margin-top: 0.4rem;
    white-space: pre-wrap; word-break: break-word;
    max-height: 400px; overflow-y: auto;
    font-size: var(--code-size);
  }

  /* ── Tool Use ──────────────────────────────── */
  .block-tool-use {
    background: var(--bg-tool);
    border: 1px solid rgba(124,92,252,0.12);
    border-radius: 6px;
    padding: 0.5rem 0.7rem;
    margin: 0.3rem 0;
  }
  .tool-header {
    display: flex; align-items: center; gap: 0.5rem;
    margin-bottom: 0.2rem;
  }
  .tool-name {
    font-family: var(--mono); font-size: 0.8rem; font-weight: 600;
    color: var(--accent);
    background: var(--accent-dim);
    padding: 0.15em 0.5em;
    border-radius: 4px;
  }
  .tool-desc {
    font-size: 0.85rem; color: var(--text-muted);
  }
  .tool-detail {
    font-family: var(--mono);
    font-size: var(--code-size);
    color: var(--text-code);
    background: rgba(0,0,0,0.3);
    border-radius: 4px;
    padding: 0.4rem 0.6rem;
    margin-top: 0.3rem;
    white-space: pre-wrap; word-break: break-all;
    max-height: 250px; overflow-y: auto;
    line-height: 1.5;
  }
  .tool-detail .diff-old { color: var(--danger); }
  .tool-detail .diff-new { color: var(--success); }

  /* ── Tool Result ───────────────────────────── */
  .block-tool-result {
    background: var(--bg-result);
    border: 1px solid rgba(255,255,255,0.04);
    border-radius: 6px;
    margin: 0.15rem 0 0.3rem;
  }
  .block-tool-result summary {
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--text-dim);
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    list-style: none;
    display: flex; align-items: center; gap: 0.35rem;
  }
  .block-tool-result summary::before { content:'▸'; transition:transform 0.2s; }
  .block-tool-result[open] summary::before { transform:rotate(90deg); }
  .result-content {
    font-family: var(--mono);
    font-size: var(--code-size);
    color: var(--text-code);
    padding: 0 0.7rem 0.5rem;
    white-space: pre-wrap; word-break: break-all;
    max-height: 300px; overflow-y: auto;
    line-height: 1.5;
  }
  .result-error { color: var(--danger); }

  .timestamp {
    font-family: var(--mono); font-size: 0.65rem; color: var(--text-dim);
  }

  /* ── Tool Response (not a real user message) ── */
  .entry-tool-response {
    padding: 0 0.9rem;
    margin: -0.2rem 0 0.2rem;
  }

  /* ── Subagent ──────────────────────────────── */
  .entry-subagent {
    border: 1px solid rgba(251,191,36,0.15);
    border-radius: 8px;
    margin: 0.5rem 0;
    background: rgba(251,191,36,0.03);
  }
  .subagent-header {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.5rem 0.9rem;
    cursor: pointer;
    list-style: none;
    font-size: 0.8rem;
  }
  .subagent-header::before { content:'▸'; color:var(--warning); transition:transform 0.2s; }
  .entry-subagent[open] > .subagent-header::before { transform:rotate(90deg); }
  .subagent-badge {
    font-family: var(--mono); font-size: 0.7rem; font-weight: 600;
    color: var(--warning);
    background: rgba(251,191,36,0.12);
    padding: 0.1em 0.45em;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .subagent-id {
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--text-dim);
  }
  .subagent-body {
    padding: 0.25rem 0.75rem 0.75rem;
    border-top: 1px solid rgba(251,191,36,0.08);
  }
  .subagent-body .entry {
    margin-bottom: 0.3rem;
  }
  .subagent-body .entry-assistant {
    border-left-color: var(--warning);
    background: rgba(251,191,36,0.04);
  }
  .subagent-body .entry-assistant .role-label { color: var(--warning); }

  /* ── Scroll button ─────────────────────────── */
  .scroll-btn {
    position: fixed; bottom: 1.25rem; right: 1.25rem;
    background: var(--accent); color: #fff;
    border: none; border-radius: 50%;
    width: 36px; height: 36px; font-size: 1.1rem;
    cursor: pointer;
    display: none; align-items: center; justify-content: center;
    box-shadow: 0 4px 15px rgba(124,92,252,0.4);
    z-index: 50;
  }
  .scroll-btn.visible { display: flex; }
</style>
</head>
<body>

<div class="header">
  <h1>Claude Code Live</h1>
  <div class="status"><span class="dot"></span> watching</div>
  <div class="controls">
    <label><input type="checkbox" id="showThinking"> thinking</label>
    <label><input type="checkbox" id="showResults" checked> results</label>
    <label><input type="checkbox" id="showSubagents" checked> subagents</label>
    <label><input type="checkbox" id="autoScroll" checked> auto-scroll</label>
    <button class="settings-btn" id="settingsToggle">style</button>
  </div>
  <div class="stats">
    <span id="statEntries">0 entries</span>
    <span id="statFile">-</span>
  </div>
</div>

<div class="settings-panel" id="settingsPanel">
  <h3>Display Settings</h3>

  <div class="setting-row">
    <label>Body font size</label>
    <input type="range" id="fontSize" min="12" max="22" value="15" step="0.5">
    <span class="val" id="fontSizeVal">15px</span>
  </div>

  <div class="setting-row">
    <label>Code font size</label>
    <input type="range" id="codeSize" min="11" max="20" value="13.5" step="0.5">
    <span class="val" id="codeSizeVal">13.5px</span>
  </div>

  <div class="setting-row">
    <label>Code text color</label>
    <input type="color" id="codeColor" value="#c8d5e4">
    <span class="val" id="codeColorVal">#c8d5e4</span>
  </div>

  <div class="setting-row">
    <label>Body text color</label>
    <input type="color" id="textColor" value="#e2e8f0">
    <span class="val" id="textColorVal">#e2e8f0</span>
  </div>

  <div class="setting-row">
    <label>Muted text color</label>
    <input type="color" id="mutedColor" value="#b8c4d4">
    <span class="val" id="mutedColorVal">#b8c4d4</span>
  </div>

  <div style="margin-top:0.75rem;">
    <label style="font-size:0.75rem;color:var(--text-dim);">Presets</label>
    <div class="preset-row">
      <button class="preset-btn" data-preset="default">Default</button>
      <button class="preset-btn" data-preset="bright">Bright</button>
      <button class="preset-btn" data-preset="large">Large</button>
      <button class="preset-btn" data-preset="compact">Compact</button>
    </div>
  </div>
</div>

<div id="timeline"></div>
<button class="scroll-btn" id="scrollBtn">&#8595;</button>

<script>
const $ = id => document.getElementById(id);
const timeline = $('timeline');
const scrollBtn = $('scrollBtn');
const showThinking = $('showThinking');
const showResults = $('showResults');
const showSubagents = $('showSubagents');
const autoScroll = $('autoScroll');
const statEntries = $('statEntries');
const statFile = $('statFile');

let cursor = 0;
let total = 0;
let atBottom = true;

window.addEventListener('scroll', () => {
  atBottom = (window.innerHeight + window.scrollY) >= document.body.offsetHeight - 80;
  scrollBtn.classList.toggle('visible', !atBottom);
});
scrollBtn.onclick = () => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function fmtTime(ts) {
  if (!ts) return '';
  try { return new Date(ts).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
  catch { return ''; }
}

function renderBlock(b) {
  if (b.type === 'text')
    return `<div class="block-text">${esc(b.text)}</div>`;

  if (b.type === 'thinking')
    return `<details class="block-thinking" data-bt="t" ${showThinking.checked?'':'style="display:none"'}>
      <summary>thinking</summary>
      <div class="thought-content">${esc(b.text)}</div></details>`;

  if (b.type === 'tool_use') {
    let detail = '';
    const t = b.tool;
    if (t === 'Bash') {
      detail = b.description ? `<div class="tool-desc">${esc(b.description)}</div>` : '';
      detail += `<div class="tool-detail">${esc(b.command||'')}</div>`;
    } else if (t === 'Read') {
      detail = `<div class="tool-detail">${esc(b.file_path||'')}</div>`;
    } else if (t === 'Write') {
      detail = `<div class="tool-detail">${esc(b.file_path||'')} <span style="color:var(--text-dim)">(${(b.content_length||0).toLocaleString()} chars)</span></div>`;
    } else if (t === 'Edit') {
      detail = `<div class="tool-detail">${esc(b.file_path||'')}\n<span class="diff-old">- ${esc(b.old_string||'')}</span>\n<span class="diff-new">+ ${esc(b.new_string||'')}</span></div>`;
    } else if (t === 'Glob') {
      detail = `<div class="tool-detail">${esc(b.pattern||'')}</div>`;
    } else if (t === 'Grep') {
      detail = `<div class="tool-detail">${esc(b.pattern||'')} in ${esc(b.path||'.')}</div>`;
    } else if (t === 'WebFetch') {
      detail = `<div class="tool-detail">${esc(b.url||'')}</div>`;
    } else if (t === 'WebSearch') {
      detail = `<div class="tool-detail">${esc(b.query||'')}</div>`;
    } else if (t === 'Task') {
      detail = `<div class="tool-desc">${esc(b.description||'')} <span style="color:var(--accent)">${esc(b.agent_type||'')}</span></div>`;
    } else if (b.input_keys && b.input_keys.length) {
      detail = `<div class="tool-detail">${esc(b.input_keys.join(', '))}</div>`;
    }
    return `<div class="block-tool-use">
      <div class="tool-header"><span class="tool-name">${esc(t)}</span></div>${detail}</div>`;
  }

  if (b.type === 'tool_result') {
    const cls = b.is_error ? 'result-content result-error' : 'result-content';
    const label = b.is_error ? 'error' : 'result';
    return `<details class="block-tool-result" data-bt="r" ${showResults.checked?'':'style="display:none"'}>
      <summary>${label}</summary>
      <div class="${cls}">${esc(b.content||'')}</div></details>`;
  }
  return '';
}

function renderEntry(e) {
  const time = fmtTime(e.timestamp);
  const ts = time ? `<span class="timestamp">${time}</span>` : '';

  if (e.type === 'subagent') {
    const inner = (e.entries||[]).map(renderEntry).join('');
    const count = (e.entries||[]).length;
    const hide = showSubagents.checked ? '' : 'style="display:none"';
    return `<details class="entry entry-subagent" data-bt="s" ${hide}>
      <summary class="subagent-header">
        <span class="subagent-badge">subagent</span>
        <span class="subagent-id">${esc(e.agent_id||'')}</span>
        <span style="color:var(--text-dim)">${count} entries</span>
        ${ts}
      </summary>
      <div class="subagent-body">${inner}</div>
    </details>`;
  }

  if (e.type === 'user_message')
    return `<div class="entry entry-user">
      <div class="role-label">user ${ts}</div>
      <div class="msg-text">${esc(e.text)}</div></div>`;

  const blocks = (e.blocks||[]).map(renderBlock).join('');
  if (e.type === 'tool_response')
    return `<div class="entry entry-tool-response">${blocks}</div>`;
  if (e.role === 'user')
    return `<div class="entry entry-user"><div class="role-label">user ${ts}</div>${blocks}</div>`;
  if (e.role === 'assistant')
    return `<div class="entry entry-assistant"><div class="role-label">claude ${ts}</div>${blocks}</div>`;
  return `<div class="entry">${blocks}</div>`;
}

async function poll() {
  try {
    const r = await fetch(`/api/entries?after=${cursor}`);
    const d = await r.json();
    if (d.entries.length) {
      const frag = document.createDocumentFragment();
      const wrap = document.createElement('div');
      wrap.innerHTML = d.entries.map(renderEntry).join('');
      while (wrap.firstChild) frag.appendChild(wrap.firstChild);
      timeline.appendChild(frag);
      total += d.entries.length;
      statEntries.textContent = `${total} entries`;
      if (autoScroll.checked && atBottom)
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    }
    cursor = d.cursor;
  } catch(e) { console.error(e); }
}

// Toggle visibility
showThinking.onchange = () =>
  document.querySelectorAll('[data-bt="t"]').forEach(el => el.style.display = showThinking.checked ? '' : 'none');
showResults.onchange = () =>
  document.querySelectorAll('[data-bt="r"]').forEach(el => el.style.display = showResults.checked ? '' : 'none');
showSubagents.onchange = () =>
  document.querySelectorAll('[data-bt="s"]').forEach(el => el.style.display = showSubagents.checked ? '' : 'none');

// ── Settings Panel ─────────────────────────────
const settingsToggle = $('settingsToggle');
const settingsPanel = $('settingsPanel');
const root = document.documentElement;

settingsToggle.onclick = () => settingsPanel.classList.toggle('open');
document.addEventListener('click', e => {
  if (!settingsPanel.contains(e.target) && e.target !== settingsToggle)
    settingsPanel.classList.remove('open');
});

function bindSlider(id, cssVar, unit, valId) {
  const slider = $(id);
  const display = $(valId);
  slider.oninput = () => {
    root.style.setProperty(cssVar, slider.value + unit);
    display.textContent = slider.value + unit;
    saveSettings();
  };
}
function bindColor(id, cssVar, valId) {
  const picker = $(id);
  const display = $(valId);
  picker.oninput = () => {
    root.style.setProperty(cssVar, picker.value);
    display.textContent = picker.value;
    saveSettings();
  };
}

bindSlider('fontSize', '--font-size', 'px', 'fontSizeVal');
bindSlider('codeSize', '--code-size', 'px', 'codeSizeVal');
bindColor('codeColor', '--text-code', 'codeColorVal');
bindColor('textColor', '--text', 'textColorVal');
bindColor('mutedColor', '--text-muted', 'mutedColorVal');

const presets = {
  default: { fontSize:15, codeSize:13.5, codeColor:'#c8d5e4', textColor:'#e2e8f0', mutedColor:'#b8c4d4' },
  bright:  { fontSize:15, codeSize:14, codeColor:'#e8eef6', textColor:'#f1f5f9', mutedColor:'#cbd5e1' },
  large:   { fontSize:18, codeSize:16, codeColor:'#dce4f0', textColor:'#f1f5f9', mutedColor:'#c8d4e2' },
  compact: { fontSize:13, codeSize:12, codeColor:'#b8c4d4', textColor:'#d4dce8', mutedColor:'#9cabb8' },
};

document.querySelectorAll('.preset-btn').forEach(btn => {
  btn.onclick = () => {
    const p = presets[btn.dataset.preset];
    if (!p) return;
    applySettings(p);
    saveSettings();
  };
});

function applySettings(s) {
  root.style.setProperty('--font-size', s.fontSize + 'px');
  root.style.setProperty('--code-size', s.codeSize + 'px');
  root.style.setProperty('--text-code', s.codeColor);
  root.style.setProperty('--text', s.textColor);
  root.style.setProperty('--text-muted', s.mutedColor);
  $('fontSize').value = s.fontSize; $('fontSizeVal').textContent = s.fontSize + 'px';
  $('codeSize').value = s.codeSize; $('codeSizeVal').textContent = s.codeSize + 'px';
  $('codeColor').value = s.codeColor; $('codeColorVal').textContent = s.codeColor;
  $('textColor').value = s.textColor; $('textColorVal').textContent = s.textColor;
  $('mutedColor').value = s.mutedColor; $('mutedColorVal').textContent = s.mutedColor;
}

function saveSettings() {
  const s = {
    fontSize: parseFloat($('fontSize').value),
    codeSize: parseFloat($('codeSize').value),
    codeColor: $('codeColor').value,
    textColor: $('textColor').value,
    mutedColor: $('mutedColor').value,
  };
  localStorage.setItem('claude-live-settings', JSON.stringify(s));
}
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('claude-live-settings'));
    if (s) applySettings(s);
  } catch {}
}
loadSettings();

// Load file info
fetch('/api/info').then(r=>r.json()).then(d => {
  statFile.textContent = d.file;
});

poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"""


# ── CLI Entry Point ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Live — real-time session transcript viewer"
    )
    parser.add_argument(
        "session", nargs="?",
        help="Path to a .jsonl session file. Default: most recent session."
    )
    parser.add_argument(
        "--port", type=int, default=7777,
        help="HTTP port (default: 7777)"
    )
    args = parser.parse_args()

    if args.session:
        session_path = Path(args.session).resolve()
        source = "provided"
    else:
        session_path = find_latest_session()
        source = "auto-detected"

    if not session_path or not session_path.exists():
        print("Error: no session file found.", file=sys.stderr)
        print("Usage: claude-code-live [path/to/session.jsonl]", file=sys.stderr)
        sys.exit(1)

    print(f"  Session:  {session_path} ({source})")
    print(f"  Size:     {session_path.stat().st_size:,} bytes")

    LiveHandler.session_path = str(session_path)
    port = args.port
    server = None
    for attempt in range(10):
        try:
            server = HTTPServer(("0.0.0.0", port), LiveHandler)
            break
        except OSError as e:
            if e.errno == 98:
                port += 1
            else:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

    if server is None:
        print(f"Error: ports {args.port}–{port} are all in use.", file=sys.stderr)
        print(f"Specify a port: claude-code-live --port PORT", file=sys.stderr)
        sys.exit(1)

    if port != args.port:
        print(f"  Note:     port {args.port} in use, using {port}")
    print(f"  Server:   http://0.0.0.0:{port}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
