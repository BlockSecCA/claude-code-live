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
from datetime import datetime
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


def pick_session():
    """Interactive session picker. If one session exists, return it.
    If multiple, show a numbered list and let the user choose.
    All prompts go to stderr so stdout stays clean."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    jsonls = [p for p in base.rglob("*.jsonl") if "/subagents/" not in str(p)]
    if not jsonls:
        return None
    if len(jsonls) == 1:
        return jsonls[0]

    # Sort by mtime descending (most recent first)
    jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    print("\nAvailable sessions:\n", file=sys.stderr)
    print(f"  {'#':>3}  {'Session UUID':<38}  {'Project':<24}  {'Size':>10}  {'Modified'}", file=sys.stderr)
    print(f"  {'─'*3}  {'─'*38}  {'─'*24}  {'─'*10}  {'─'*19}", file=sys.stderr)

    for i, p in enumerate(jsonls, 1):
        st = p.stat()
        size = st.st_size
        if size >= 1_048_576:
            size_str = f"{size / 1_048_576:.1f} MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        project = p.parent.name
        print(f"  {i:>3}  {p.stem:<38}  {project:<24}  {size_str:>10}  {mtime}", file=sys.stderr)

    print(file=sys.stderr)
    try:
        choice = input("Pick a session [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None

    if not choice:
        idx = 0
    else:
        try:
            idx = int(choice) - 1
        except ValueError:
            print(f"Invalid choice: {choice}", file=sys.stderr)
            return None

    if idx < 0 or idx >= len(jsonls):
        print(f"Out of range: {idx + 1}", file=sys.stderr)
        return None

    return jsonls[idx]


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
    poll_interval_ms = 1500

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            html = HTML.replace("__POLL_INTERVAL_MS__", str(self.poll_interval_ms))
            self._respond(200, "text/html", html)
        elif parsed.path == "/api/entries":
            if not Path(self.session_path).exists():
                self._respond(200, "application/json", json.dumps({
                    "gone": True
                }))
                return
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
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


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
  .msg-text { word-break: break-word; }

  /* ── Assistant ─────────────────────────────── */
  .entry-assistant {
    background: var(--bg-card);
    border-left: 3px solid var(--accent);
    border-radius: 0 8px 8px 0;
    padding: 0.6rem 0.9rem;
  }
  .entry-assistant .role-label { color: var(--accent); }

  /* ── Text (rendered markdown) ───────────────── */
  .block-text {
    word-break: break-word;
    margin: 0.2rem 0;
  }
  .block-text p { margin: 0.4em 0; }
  .block-text h1, .block-text h2, .block-text h3,
  .block-text h4, .block-text h5, .block-text h6 {
    margin: 0.8em 0 0.3em; color: var(--text); font-weight: 600;
  }
  .block-text h1 { font-size: 1.4em; }
  .block-text h2 { font-size: 1.2em; }
  .block-text h3 { font-size: 1.05em; }
  .block-text ul, .block-text ol {
    margin: 0.3em 0; padding-left: 1.5em;
  }
  .block-text li { margin: 0.15em 0; }
  .block-text code {
    font-family: var(--mono); font-size: var(--code-size);
    background: rgba(255,255,255,0.06); padding: 0.15em 0.35em;
    border-radius: 3px; color: var(--text-code);
  }
  .block-text pre {
    background: var(--bg-result); border: 1px solid var(--border);
    border-radius: 6px; padding: 0.6em 0.8em; margin: 0.4em 0;
    overflow-x: auto;
  }
  .block-text pre code {
    background: none; padding: 0; font-size: var(--code-size);
  }
  .block-text table {
    border-collapse: collapse; margin: 0.5em 0; width: 100%;
  }
  .block-text th, .block-text td {
    border: 1px solid var(--border); padding: 0.35em 0.6em;
    text-align: left;
  }
  .block-text th {
    background: rgba(255,255,255,0.04); font-weight: 600;
  }
  .block-text blockquote {
    border-left: 3px solid var(--accent); padding-left: 0.8em;
    margin: 0.4em 0; color: var(--text-muted);
  }
  .block-text a { color: var(--accent); text-decoration: none; }
  .block-text a:hover { text-decoration: underline; }
  .block-text hr {
    border: none; border-top: 1px solid var(--border); margin: 0.8em 0;
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

  <div style="margin-top:0.5rem;">
    <label style="font-size:0.75rem;color:var(--text-dim);">Theme</label>
    <div class="preset-row" style="margin-top:0.3rem;">
      <button class="preset-btn theme-btn" data-theme="dark">Dark</button>
      <button class="preset-btn theme-btn" data-theme="mid">Mid</button>
      <button class="preset-btn theme-btn" data-theme="light">Light</button>
    </div>
  </div>
</div>

<div id="timeline"></div>
<button class="scroll-btn" id="scrollBtn">&#8595;</button>

<script id="marked-lib">
/**
 * marked v15.0.7 - a markdown parser
 * Copyright (c) 2011-2025, Christopher Jeffrey. (MIT Licensed)
 * https://github.com/markedjs/marked
 */
!function(e,t){"object"==typeof exports&&"undefined"!=typeof module?t(exports):"function"==typeof define&&define.amd?define(["exports"],t):t((e="undefined"!=typeof globalThis?globalThis:e||self).marked={})}(this,(function(e){"use strict";function t(){return{async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null}}function n(t){e.defaults=t}e.defaults={async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null};const s={exec:()=>null};function r(e,t=""){let n="string"==typeof e?e:e.source;const s={replace:(e,t)=>{let r="string"==typeof t?t:t.source;return r=r.replace(i.caret,"$1"),n=n.replace(e,r),s},getRegex:()=>new RegExp(n,t)};return s}const i={codeRemoveIndent:/^(?: {1,4}| {0,3}\t)/gm,outputLinkReplace:/\\([\[\]])/g,indentCodeCompensation:/^(\s+)(?:```)/,beginningSpace:/^\s+/,endingHash:/#$/,startingSpaceChar:/^ /,endingSpaceChar:/ $/,nonSpaceChar:/[^ ]/,newLineCharGlobal:/\n/g,tabCharGlobal:/\t/g,multipleSpaceGlobal:/\s+/g,blankLine:/^[ \t]*$/,doubleBlankLine:/\n[ \t]*\n[ \t]*$/,blockquoteStart:/^ {0,3}>/,blockquoteSetextReplace:/\n {0,3}((?:=+|-+) *)(?=\n|$)/g,blockquoteSetextReplace2:/^ {0,3}>[ \t]?/gm,listReplaceTabs:/^\t+/,listReplaceNesting:/^ {1,4}(?=( {4})*[^ ])/g,listIsTask:/^\[[ xX]\] /,listReplaceTask:/^\[[ xX]\] +/,anyLine:/\n.*\n/,hrefBrackets:/^<(.*)>$/,tableDelimiter:/[:|]/,tableAlignChars:/^\||\| *$/g,tableRowBlankLine:/\n[ \t]*$/,tableAlignRight:/^ *-+: *$/,tableAlignCenter:/^ *:-+: *$/,tableAlignLeft:/^ *:-+ *$/,startATag:/^<a /i,endATag:/^<\/a>/i,startPreScriptTag:/^<(pre|code|kbd|script)(\s|>)/i,endPreScriptTag:/^<\/(pre|code|kbd|script)(\s|>)/i,startAngleBracket:/^</,endAngleBracket:/>$/,pedanticHrefTitle:/^([^'"]*[^\s])\s+(['"])(.*)\2/,unicodeAlphaNumeric:/[\p{L}\p{N}]/u,escapeTest:/[&<>"']/,escapeReplace:/[&<>"']/g,escapeTestNoEncode:/[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)/,escapeReplaceNoEncode:/[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)/g,unescapeTest:/&(#(?:\d+)|(?:#x[0-9A-Fa-f]+)|(?:\w+));?/gi,caret:/(^|[^\[])\^/g,percentDecode:/%25/g,findPipe:/\|/g,splitPipe:/ \|/,slashPipe:/\\\|/g,carriageReturn:/\r\n|\r/g,spaceLine:/^ +$/gm,notSpaceStart:/^\S*/,endingNewline:/\n$/,listItemRegex:e=>new RegExp(`^( {0,3}${e})((?:[\t ][^\\n]*)?(?:\\n|$))`),nextBulletRegex:e=>new RegExp(`^ {0,${Math.min(3,e-1)}}(?:[*+-]|\\d{1,9}[.)])((?:[ \t][^\\n]*)?(?:\\n|$))`),hrRegex:e=>new RegExp(`^ {0,${Math.min(3,e-1)}}((?:- *){3,}|(?:_ *){3,}|(?:\\* *){3,})(?:\\n+|$)`),fencesBeginRegex:e=>new RegExp(`^ {0,${Math.min(3,e-1)}}(?:\`\`\`|~~~)`),headingBeginRegex:e=>new RegExp(`^ {0,${Math.min(3,e-1)}}#`),htmlBeginRegex:e=>new RegExp(`^ {0,${Math.min(3,e-1)}}<(?:[a-z].*>|!--)`,"i")},l=/^ {0,3}((?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)/,o=/(?:[*+-]|\d{1,9}[.)])/,a=/^(?!bull |blockCode|fences|blockquote|heading|html|table)((?:.|\n(?!\s*?\n|bull |blockCode|fences|blockquote|heading|html|table))+?)\n {0,3}(=+|-+) *(?:\n+|$)/,c=r(a).replace(/bull/g,o).replace(/blockCode/g,/(?: {4}| {0,3}\t)/).replace(/fences/g,/ {0,3}(?:`{3,}|~{3,})/).replace(/blockquote/g,/ {0,3}>/).replace(/heading/g,/ {0,3}#{1,6}/).replace(/html/g,/ {0,3}<[^\n>]+>\n/).replace(/\|table/g,"").getRegex(),h=r(a).replace(/bull/g,o).replace(/blockCode/g,/(?: {4}| {0,3}\t)/).replace(/fences/g,/ {0,3}(?:`{3,}|~{3,})/).replace(/blockquote/g,/ {0,3}>/).replace(/heading/g,/ {0,3}#{1,6}/).replace(/html/g,/ {0,3}<[^\n>]+>\n/).replace(/table/g,/ {0,3}\|?(?:[:\- ]*\|)+[\:\- ]*\n/).getRegex(),p=/^([^\n]+(?:\n(?!hr|heading|lheading|blockquote|fences|list|html|table| +\n)[^\n]+)*)/,u=/(?!\s*\])(?:\\.|[^\[\]\\])+/,g=r(/^ {0,3}\[(label)\]: *(?:\n[ \t]*)?([^<\s][^\s]*|<.*?>)(?:(?: +(?:\n[ \t]*)?| *\n[ \t]*)(title))? *(?:\n+|$)/).replace("label",u).replace("title",/(?:"(?:\\"?|[^"\\])*"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\))/).getRegex(),k=r(/^( {0,3}bull)([ \t][^\n]+?)?(?:\n|$)/).replace(/bull/g,o).getRegex(),d="address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul",f=/<!--(?:-?>|[\s\S]*?(?:-->|$))/,x=r("^ {0,3}(?:<(script|pre|style|textarea)[\\s>][\\s\\S]*?(?:</\\1>[^\\n]*\\n+|$)|comment[^\\n]*(\\n+|$)|<\\?[\\s\\S]*?(?:\\?>\\n*|$)|<![A-Z][\\s\\S]*?(?:>\\n*|$)|<!\\[CDATA\\[[\\s\\S]*?(?:\\]\\]>\\n*|$)|</?(tag)(?: +|\\n|/?>)[\\s\\S]*?(?:(?:\\n[ \t]*)+\\n|$)|<(?!script|pre|style|textarea)([a-z][\\w-]*)(?:attribute)*? */?>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n[ \t]*)+\\n|$)|</(?!script|pre|style|textarea)[a-z][\\w-]*\\s*>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n[ \t]*)+\\n|$))","i").replace("comment",f).replace("tag",d).replace("attribute",/ +[a-zA-Z:_][\w.:-]*(?: *= *"[^"\n]*"| *= *'[^'\n]*'| *= *[^\s"'=<>`]+)?/).getRegex(),b=r(p).replace("hr",l).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("|lheading","").replace("|table","").replace("blockquote"," {0,3}>").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",d).getRegex(),w={blockquote:r(/^( {0,3}> ?(paragraph|[^\n]*)(?:\n|$))+/).replace("paragraph",b).getRegex(),code:/^((?: {4}| {0,3}\t)[^\n]+(?:\n(?:[ \t]*(?:\n|$))*)?)+/,def:g,fences:/^ {0,3}(`{3,}(?=[^`\n]*(?:\n|$))|~{3,})([^\n]*)(?:\n|$)(?:|([\s\S]*?)(?:\n|$))(?: {0,3}\1[~`]* *(?=\n|$)|$)/,heading:/^ {0,3}(#{1,6})(?=\s|$)(.*)(?:\n+|$)/,hr:l,html:x,lheading:c,list:k,newline:/^(?:[ \t]*(?:\n|$))+/,paragraph:b,table:s,text:/^[^\n]+/},m=r("^ *([^\\n ].*)\\n {0,3}((?:\\| *)?:?-+:? *(?:\\| *:?-+:? *)*(?:\\| *)?)(?:\\n((?:(?! *\\n|hr|heading|blockquote|code|fences|list|html).*(?:\\n|$))*)\\n*|$)").replace("hr",l).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("blockquote"," {0,3}>").replace("code","(?: {4}| {0,3}\t)[^\\n]").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",d).getRegex(),y={...w,lheading:h,table:m,paragraph:r(p).replace("hr",l).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("|lheading","").replace("table",m).replace("blockquote"," {0,3}>").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",d).getRegex()},$={...w,html:r("^ *(?:comment *(?:\\n|\\s*$)|<(tag)[\\s\\S]+?</\\1> *(?:\\n{2,}|\\s*$)|<tag(?:\"[^\"]*\"|'[^']*'|\\s[^'\"/>\\s]*)*?/?> *(?:\\n{2,}|\\s*$))").replace("comment",f).replace(/tag/g,"(?!(?:a|em|strong|small|s|cite|q|dfn|abbr|data|time|code|var|samp|kbd|sub|sup|i|b|u|mark|ruby|rt|rp|bdi|bdo|span|br|wbr|ins|del|img)\\b)\\w+(?!:|[^\\w\\s@]*@)\\b").getRegex(),def:/^ *\[([^\]]+)\]: *<?([^\s>]+)>?(?: +(["(][^\n]+[")]))? *(?:\n+|$)/,heading:/^(#{1,6})(.*)(?:\n+|$)/,fences:s,lheading:/^(.+?)\n {0,3}(=+|-+) *(?:\n+|$)/,paragraph:r(p).replace("hr",l).replace("heading"," *#{1,6} *[^\n]").replace("lheading",c).replace("|table","").replace("blockquote"," {0,3}>").replace("|fences","").replace("|list","").replace("|html","").replace("|tag","").getRegex()},R=/^( {2,}|\\)\n(?!\s*$)/,S=/[\p{P}\p{S}]/u,T=/[\s\p{P}\p{S}]/u,z=/[^\s\p{P}\p{S}]/u,A=r(/^((?![*_])punctSpace)/,"u").replace(/punctSpace/g,T).getRegex(),_=/(?!~)[\p{P}\p{S}]/u,P=/^(?:\*+(?:((?!\*)punct)|[^\s*]))|^_+(?:((?!_)punct)|([^\s_]))/,I=r(P,"u").replace(/punct/g,S).getRegex(),L=r(P,"u").replace(/punct/g,_).getRegex(),B="^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)|[^*]+(?=[^*])|(?!\\*)punct(\\*+)(?=[\\s]|$)|notPunctSpace(\\*+)(?!\\*)(?=punctSpace|$)|(?!\\*)punctSpace(\\*+)(?=notPunctSpace)|[\\s](\\*+)(?!\\*)(?=punct)|(?!\\*)punct(\\*+)(?!\\*)(?=punct)|notPunctSpace(\\*+)(?=notPunctSpace)",C=r(B,"gu").replace(/notPunctSpace/g,z).replace(/punctSpace/g,T).replace(/punct/g,S).getRegex(),q=r(B,"gu").replace(/notPunctSpace/g,/(?:[^\s\p{P}\p{S}]|~)/u).replace(/punctSpace/g,/(?!~)[\s\p{P}\p{S}]/u).replace(/punct/g,_).getRegex(),E=r("^[^_*]*?\\*\\*[^_*]*?_[^_*]*?(?=\\*\\*)|[^_]+(?=[^_])|(?!_)punct(_+)(?=[\\s]|$)|notPunctSpace(_+)(?!_)(?=punctSpace|$)|(?!_)punctSpace(_+)(?=notPunctSpace)|[\\s](_+)(?!_)(?=punct)|(?!_)punct(_+)(?!_)(?=punct)","gu").replace(/notPunctSpace/g,z).replace(/punctSpace/g,T).replace(/punct/g,S).getRegex(),Z=r(/\\(punct)/,"gu").replace(/punct/g,S).getRegex(),v=r(/^<(scheme:[^\s\x00-\x1f<>]*|email)>/).replace("scheme",/[a-zA-Z][a-zA-Z0-9+.-]{1,31}/).replace("email",/[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+(@)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+(?![-_])/).getRegex(),D=r(f).replace("(?:--\x3e|$)","--\x3e").getRegex(),M=r("^comment|^</[a-zA-Z][\\w:-]*\\s*>|^<[a-zA-Z][\\w-]*(?:attribute)*?\\s*/?>|^<\\?[\\s\\S]*?\\?>|^<![a-zA-Z]+\\s[\\s\\S]*?>|^<!\\[CDATA\\[[\\s\\S]*?\\]\\]>").replace("comment",D).replace("attribute",/\s+[a-zA-Z:_][\w.:-]*(?:\s*=\s*"[^"]*"|\s*=\s*'[^']*'|\s*=\s*[^\s"'=<>`]+)?/).getRegex(),O=/(?:\[(?:\\.|[^\[\]\\])*\]|\\.|`[^`]*`|[^\[\]\\`])*?/,Q=r(/^!?\[(label)\]\(\s*(href)(?:\s+(title))?\s*\)/).replace("label",O).replace("href",/<(?:\\.|[^\n<>\\])+>|[^\s\x00-\x1f]*/).replace("title",/"(?:\\"?|[^"\\])*"|'(?:\\'?|[^'\\])*'|\((?:\\\)?|[^)\\])*\)/).getRegex(),j=r(/^!?\[(label)\]\[(ref)\]/).replace("label",O).replace("ref",u).getRegex(),N=r(/^!?\[(ref)\](?:\[\])?/).replace("ref",u).getRegex(),G={_backpedal:s,anyPunctuation:Z,autolink:v,blockSkip:/\[[^[\]]*?\]\((?:\\.|[^\\\(\)]|\((?:\\.|[^\\\(\)])*\))*\)|`[^`]*?`|<[^<>]*?>/g,br:R,code:/^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/,del:s,emStrongLDelim:I,emStrongRDelimAst:C,emStrongRDelimUnd:E,escape:/^\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])/,link:Q,nolink:N,punctuation:A,reflink:j,reflinkSearch:r("reflink|nolink(?!\\()","g").replace("reflink",j).replace("nolink",N).getRegex(),tag:M,text:/^(`+|[^`])(?:(?= {2,}\n)|[\s\S]*?(?:(?=[\\<!\[`*_]|\b_|$)|[^ ](?= {2,}\n)))/,url:s},H={...G,link:r(/^!?\[(label)\]\((.*?)\)/).replace("label",O).getRegex(),reflink:r(/^!?\[(label)\]\s*\[([^\]]*)\]/).replace("label",O).getRegex()},X={...G,emStrongRDelimAst:q,emStrongLDelim:L,url:r(/^((?:ftp|https?):\/\/|www\.)(?:[a-zA-Z0-9\-]+\.?)+[^\s<]*|^email/,"i").replace("email",/[A-Za-z0-9._+-]+(@)[a-zA-Z0-9-_]+(?:\.[a-zA-Z0-9-_]*[a-zA-Z0-9])+(?![-_])/).getRegex(),_backpedal:/(?:[^?!.,:;*_'"~()&]+|\([^)]*\)|&(?![a-zA-Z0-9]+;$)|[?!.,:;*_'"~)]+(?!$))+/,del:/^(~~?)(?=[^\s~])((?:\\.|[^\\])*?(?:\\.|[^\s~\\]))\1(?=[^~]|$)/,text:/^([`~]+|[^`~])(?:(?= {2,}\n)|(?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)|[\s\S]*?(?:(?=[\\<!\[`*~_]|\b_|https?:\/\/|ftp:\/\/|www\.|$)|[^ ](?= {2,}\n)|[^a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-](?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)))/},F={...X,br:r(R).replace("{2,}","*").getRegex(),text:r(X.text).replace("\\b_","\\b_| {2,}\\n").replace(/\{2,\}/g,"*").getRegex()},U={normal:w,gfm:y,pedantic:$},J={normal:G,gfm:X,breaks:F,pedantic:H},K={"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"},V=e=>K[e];function W(e,t){if(t){if(i.escapeTest.test(e))return e.replace(i.escapeReplace,V)}else if(i.escapeTestNoEncode.test(e))return e.replace(i.escapeReplaceNoEncode,V);return e}function Y(e){try{e=encodeURI(e).replace(i.percentDecode,"%")}catch{return null}return e}function ee(e,t){const n=e.replace(i.findPipe,((e,t,n)=>{let s=!1,r=t;for(;--r>=0&&"\\"===n[r];)s=!s;return s?"|":" |"})).split(i.splitPipe);let s=0;if(n[0].trim()||n.shift(),n.length>0&&!n.at(-1)?.trim()&&n.pop(),t)if(n.length>t)n.splice(t);else for(;n.length<t;)n.push("");for(;s<n.length;s++)n[s]=n[s].trim().replace(i.slashPipe,"|");return n}function te(e,t,n){const s=e.length;if(0===s)return"";let r=0;for(;r<s;){if(e.charAt(s-r-1)!==t)break;r++}return e.slice(0,s-r)}function ne(e,t,n,s,r){const i=t.href,l=t.title||null,o=e[1].replace(r.other.outputLinkReplace,"$1");if("!"!==e[0].charAt(0)){s.state.inLink=!0;const e={type:"link",raw:n,href:i,title:l,text:o,tokens:s.inlineTokens(o)};return s.state.inLink=!1,e}return{type:"image",raw:n,href:i,title:l,text:o}}class se{options;rules;lexer;constructor(t){this.options=t||e.defaults}space(e){const t=this.rules.block.newline.exec(e);if(t&&t[0].length>0)return{type:"space",raw:t[0]}}code(e){const t=this.rules.block.code.exec(e);if(t){const e=t[0].replace(this.rules.other.codeRemoveIndent,"");return{type:"code",raw:t[0],codeBlockStyle:"indented",text:this.options.pedantic?e:te(e,"\n")}}}fences(e){const t=this.rules.block.fences.exec(e);if(t){const e=t[0],n=function(e,t,n){const s=e.match(n.other.indentCodeCompensation);if(null===s)return t;const r=s[1];return t.split("\n").map((e=>{const t=e.match(n.other.beginningSpace);if(null===t)return e;const[s]=t;return s.length>=r.length?e.slice(r.length):e})).join("\n")}(e,t[3]||"",this.rules);return{type:"code",raw:e,lang:t[2]?t[2].trim().replace(this.rules.inline.anyPunctuation,"$1"):t[2],text:n}}}heading(e){const t=this.rules.block.heading.exec(e);if(t){let e=t[2].trim();if(this.rules.other.endingHash.test(e)){const t=te(e,"#");this.options.pedantic?e=t.trim():t&&!this.rules.other.endingSpaceChar.test(t)||(e=t.trim())}return{type:"heading",raw:t[0],depth:t[1].length,text:e,tokens:this.lexer.inline(e)}}}hr(e){const t=this.rules.block.hr.exec(e);if(t)return{type:"hr",raw:te(t[0],"\n")}}blockquote(e){const t=this.rules.block.blockquote.exec(e);if(t){let e=te(t[0],"\n").split("\n"),n="",s="";const r=[];for(;e.length>0;){let t=!1;const i=[];let l;for(l=0;l<e.length;l++)if(this.rules.other.blockquoteStart.test(e[l]))i.push(e[l]),t=!0;else{if(t)break;i.push(e[l])}e=e.slice(l);const o=i.join("\n"),a=o.replace(this.rules.other.blockquoteSetextReplace,"\n    $1").replace(this.rules.other.blockquoteSetextReplace2,"");n=n?`${n}\n${o}`:o,s=s?`${s}\n${a}`:a;const c=this.lexer.state.top;if(this.lexer.state.top=!0,this.lexer.blockTokens(a,r,!0),this.lexer.state.top=c,0===e.length)break;const h=r.at(-1);if("code"===h?.type)break;if("blockquote"===h?.type){const t=h,i=t.raw+"\n"+e.join("\n"),l=this.blockquote(i);r[r.length-1]=l,n=n.substring(0,n.length-t.raw.length)+l.raw,s=s.substring(0,s.length-t.text.length)+l.text;break}if("list"!==h?.type);else{const t=h,i=t.raw+"\n"+e.join("\n"),l=this.list(i);r[r.length-1]=l,n=n.substring(0,n.length-h.raw.length)+l.raw,s=s.substring(0,s.length-t.raw.length)+l.raw,e=i.substring(r.at(-1).raw.length).split("\n")}}return{type:"blockquote",raw:n,tokens:r,text:s}}}list(e){let t=this.rules.block.list.exec(e);if(t){let n=t[1].trim();const s=n.length>1,r={type:"list",raw:"",ordered:s,start:s?+n.slice(0,-1):"",loose:!1,items:[]};n=s?`\\d{1,9}\\${n.slice(-1)}`:`\\${n}`,this.options.pedantic&&(n=s?n:"[*+-]");const i=this.rules.other.listItemRegex(n);let l=!1;for(;e;){let n=!1,s="",o="";if(!(t=i.exec(e)))break;if(this.rules.block.hr.test(e))break;s=t[0],e=e.substring(s.length);let a=t[2].split("\n",1)[0].replace(this.rules.other.listReplaceTabs,(e=>" ".repeat(3*e.length))),c=e.split("\n",1)[0],h=!a.trim(),p=0;if(this.options.pedantic?(p=2,o=a.trimStart()):h?p=t[1].length+1:(p=t[2].search(this.rules.other.nonSpaceChar),p=p>4?1:p,o=a.slice(p),p+=t[1].length),h&&this.rules.other.blankLine.test(c)&&(s+=c+"\n",e=e.substring(c.length+1),n=!0),!n){const t=this.rules.other.nextBulletRegex(p),n=this.rules.other.hrRegex(p),r=this.rules.other.fencesBeginRegex(p),i=this.rules.other.headingBeginRegex(p),l=this.rules.other.htmlBeginRegex(p);for(;e;){const u=e.split("\n",1)[0];let g;if(c=u,this.options.pedantic?(c=c.replace(this.rules.other.listReplaceNesting,"  "),g=c):g=c.replace(this.rules.other.tabCharGlobal,"    "),r.test(c))break;if(i.test(c))break;if(l.test(c))break;if(t.test(c))break;if(n.test(c))break;if(g.search(this.rules.other.nonSpaceChar)>=p||!c.trim())o+="\n"+g.slice(p);else{if(h)break;if(a.replace(this.rules.other.tabCharGlobal,"    ").search(this.rules.other.nonSpaceChar)>=4)break;if(r.test(a))break;if(i.test(a))break;if(n.test(a))break;o+="\n"+c}h||c.trim()||(h=!0),s+=u+"\n",e=e.substring(u.length+1),a=g.slice(p)}}r.loose||(l?r.loose=!0:this.rules.other.doubleBlankLine.test(s)&&(l=!0));let u,g=null;this.options.gfm&&(g=this.rules.other.listIsTask.exec(o),g&&(u="[ ] "!==g[0],o=o.replace(this.rules.other.listReplaceTask,""))),r.items.push({type:"list_item",raw:s,task:!!g,checked:u,loose:!1,text:o,tokens:[]}),r.raw+=s}const o=r.items.at(-1);if(!o)return;o.raw=o.raw.trimEnd(),o.text=o.text.trimEnd(),r.raw=r.raw.trimEnd();for(let e=0;e<r.items.length;e++)if(this.lexer.state.top=!1,r.items[e].tokens=this.lexer.blockTokens(r.items[e].text,[]),!r.loose){const t=r.items[e].tokens.filter((e=>"space"===e.type)),n=t.length>0&&t.some((e=>this.rules.other.anyLine.test(e.raw)));r.loose=n}if(r.loose)for(let e=0;e<r.items.length;e++)r.items[e].loose=!0;return r}}html(e){const t=this.rules.block.html.exec(e);if(t){return{type:"html",block:!0,raw:t[0],pre:"pre"===t[1]||"script"===t[1]||"style"===t[1],text:t[0]}}}def(e){const t=this.rules.block.def.exec(e);if(t){const e=t[1].toLowerCase().replace(this.rules.other.multipleSpaceGlobal," "),n=t[2]?t[2].replace(this.rules.other.hrefBrackets,"$1").replace(this.rules.inline.anyPunctuation,"$1"):"",s=t[3]?t[3].substring(1,t[3].length-1).replace(this.rules.inline.anyPunctuation,"$1"):t[3];return{type:"def",tag:e,raw:t[0],href:n,title:s}}}table(e){const t=this.rules.block.table.exec(e);if(!t)return;if(!this.rules.other.tableDelimiter.test(t[2]))return;const n=ee(t[1]),s=t[2].replace(this.rules.other.tableAlignChars,"").split("|"),r=t[3]?.trim()?t[3].replace(this.rules.other.tableRowBlankLine,"").split("\n"):[],i={type:"table",raw:t[0],header:[],align:[],rows:[]};if(n.length===s.length){for(const e of s)this.rules.other.tableAlignRight.test(e)?i.align.push("right"):this.rules.other.tableAlignCenter.test(e)?i.align.push("center"):this.rules.other.tableAlignLeft.test(e)?i.align.push("left"):i.align.push(null);for(let e=0;e<n.length;e++)i.header.push({text:n[e],tokens:this.lexer.inline(n[e]),header:!0,align:i.align[e]});for(const e of r)i.rows.push(ee(e,i.header.length).map(((e,t)=>({text:e,tokens:this.lexer.inline(e),header:!1,align:i.align[t]}))));return i}}lheading(e){const t=this.rules.block.lheading.exec(e);if(t)return{type:"heading",raw:t[0],depth:"="===t[2].charAt(0)?1:2,text:t[1],tokens:this.lexer.inline(t[1])}}paragraph(e){const t=this.rules.block.paragraph.exec(e);if(t){const e="\n"===t[1].charAt(t[1].length-1)?t[1].slice(0,-1):t[1];return{type:"paragraph",raw:t[0],text:e,tokens:this.lexer.inline(e)}}}text(e){const t=this.rules.block.text.exec(e);if(t)return{type:"text",raw:t[0],text:t[0],tokens:this.lexer.inline(t[0])}}escape(e){const t=this.rules.inline.escape.exec(e);if(t)return{type:"escape",raw:t[0],text:t[1]}}tag(e){const t=this.rules.inline.tag.exec(e);if(t)return!this.lexer.state.inLink&&this.rules.other.startATag.test(t[0])?this.lexer.state.inLink=!0:this.lexer.state.inLink&&this.rules.other.endATag.test(t[0])&&(this.lexer.state.inLink=!1),!this.lexer.state.inRawBlock&&this.rules.other.startPreScriptTag.test(t[0])?this.lexer.state.inRawBlock=!0:this.lexer.state.inRawBlock&&this.rules.other.endPreScriptTag.test(t[0])&&(this.lexer.state.inRawBlock=!1),{type:"html",raw:t[0],inLink:this.lexer.state.inLink,inRawBlock:this.lexer.state.inRawBlock,block:!1,text:t[0]}}link(e){const t=this.rules.inline.link.exec(e);if(t){const e=t[2].trim();if(!this.options.pedantic&&this.rules.other.startAngleBracket.test(e)){if(!this.rules.other.endAngleBracket.test(e))return;const t=te(e.slice(0,-1),"\\");if((e.length-t.length)%2==0)return}else{const e=function(e,t){if(-1===e.indexOf(t[1]))return-1;let n=0;for(let s=0;s<e.length;s++)if("\\"===e[s])s++;else if(e[s]===t[0])n++;else if(e[s]===t[1]&&(n--,n<0))return s;return-1}(t[2],"()");if(e>-1){const n=(0===t[0].indexOf("!")?5:4)+t[1].length+e;t[2]=t[2].substring(0,e),t[0]=t[0].substring(0,n).trim(),t[3]=""}}let n=t[2],s="";if(this.options.pedantic){const e=this.rules.other.pedanticHrefTitle.exec(n);e&&(n=e[1],s=e[3])}else s=t[3]?t[3].slice(1,-1):"";return n=n.trim(),this.rules.other.startAngleBracket.test(n)&&(n=this.options.pedantic&&!this.rules.other.endAngleBracket.test(e)?n.slice(1):n.slice(1,-1)),ne(t,{href:n?n.replace(this.rules.inline.anyPunctuation,"$1"):n,title:s?s.replace(this.rules.inline.anyPunctuation,"$1"):s},t[0],this.lexer,this.rules)}}reflink(e,t){let n;if((n=this.rules.inline.reflink.exec(e))||(n=this.rules.inline.nolink.exec(e))){const e=t[(n[2]||n[1]).replace(this.rules.other.multipleSpaceGlobal," ").toLowerCase()];if(!e){const e=n[0].charAt(0);return{type:"text",raw:e,text:e}}return ne(n,e,n[0],this.lexer,this.rules)}}emStrong(e,t,n=""){let s=this.rules.inline.emStrongLDelim.exec(e);if(!s)return;if(s[3]&&n.match(this.rules.other.unicodeAlphaNumeric))return;if(!(s[1]||s[2]||"")||!n||this.rules.inline.punctuation.exec(n)){const n=[...s[0]].length-1;let r,i,l=n,o=0;const a="*"===s[0][0]?this.rules.inline.emStrongRDelimAst:this.rules.inline.emStrongRDelimUnd;for(a.lastIndex=0,t=t.slice(-1*e.length+n);null!=(s=a.exec(t));){if(r=s[1]||s[2]||s[3]||s[4]||s[5]||s[6],!r)continue;if(i=[...r].length,s[3]||s[4]){l+=i;continue}if((s[5]||s[6])&&n%3&&!((n+i)%3)){o+=i;continue}if(l-=i,l>0)continue;i=Math.min(i,i+l+o);const t=[...s[0]][0].length,a=e.slice(0,n+s.index+t+i);if(Math.min(n,i)%2){const e=a.slice(1,-1);return{type:"em",raw:a,text:e,tokens:this.lexer.inlineTokens(e)}}const c=a.slice(2,-2);return{type:"strong",raw:a,text:c,tokens:this.lexer.inlineTokens(c)}}}}codespan(e){const t=this.rules.inline.code.exec(e);if(t){let e=t[2].replace(this.rules.other.newLineCharGlobal," ");const n=this.rules.other.nonSpaceChar.test(e),s=this.rules.other.startingSpaceChar.test(e)&&this.rules.other.endingSpaceChar.test(e);return n&&s&&(e=e.substring(1,e.length-1)),{type:"codespan",raw:t[0],text:e}}}br(e){const t=this.rules.inline.br.exec(e);if(t)return{type:"br",raw:t[0]}}del(e){const t=this.rules.inline.del.exec(e);if(t)return{type:"del",raw:t[0],text:t[2],tokens:this.lexer.inlineTokens(t[2])}}autolink(e){const t=this.rules.inline.autolink.exec(e);if(t){let e,n;return"@"===t[2]?(e=t[1],n="mailto:"+e):(e=t[1],n=e),{type:"link",raw:t[0],text:e,href:n,tokens:[{type:"text",raw:e,text:e}]}}}url(e){let t;if(t=this.rules.inline.url.exec(e)){let e,n;if("@"===t[2])e=t[0],n="mailto:"+e;else{let s;do{s=t[0],t[0]=this.rules.inline._backpedal.exec(t[0])?.[0]??""}while(s!==t[0]);e=t[0],n="www."===t[1]?"http://"+t[0]:t[0]}return{type:"link",raw:t[0],text:e,href:n,tokens:[{type:"text",raw:e,text:e}]}}}inlineText(e){const t=this.rules.inline.text.exec(e);if(t){const e=this.lexer.state.inRawBlock;return{type:"text",raw:t[0],text:t[0],escaped:e}}}}class re{tokens;options;state;tokenizer;inlineQueue;constructor(t){this.tokens=[],this.tokens.links=Object.create(null),this.options=t||e.defaults,this.options.tokenizer=this.options.tokenizer||new se,this.tokenizer=this.options.tokenizer,this.tokenizer.options=this.options,this.tokenizer.lexer=this,this.inlineQueue=[],this.state={inLink:!1,inRawBlock:!1,top:!0};const n={other:i,block:U.normal,inline:J.normal};this.options.pedantic?(n.block=U.pedantic,n.inline=J.pedantic):this.options.gfm&&(n.block=U.gfm,this.options.breaks?n.inline=J.breaks:n.inline=J.gfm),this.tokenizer.rules=n}static get rules(){return{block:U,inline:J}}static lex(e,t){return new re(t).lex(e)}static lexInline(e,t){return new re(t).inlineTokens(e)}lex(e){e=e.replace(i.carriageReturn,"\n"),this.blockTokens(e,this.tokens);for(let e=0;e<this.inlineQueue.length;e++){const t=this.inlineQueue[e];this.inlineTokens(t.src,t.tokens)}return this.inlineQueue=[],this.tokens}blockTokens(e,t=[],n=!1){for(this.options.pedantic&&(e=e.replace(i.tabCharGlobal,"    ").replace(i.spaceLine,""));e;){let s;if(this.options.extensions?.block?.some((n=>!!(s=n.call({lexer:this},e,t))&&(e=e.substring(s.raw.length),t.push(s),!0))))continue;if(s=this.tokenizer.space(e)){e=e.substring(s.raw.length);const n=t.at(-1);1===s.raw.length&&void 0!==n?n.raw+="\n":t.push(s);continue}if(s=this.tokenizer.code(e)){e=e.substring(s.raw.length);const n=t.at(-1);"paragraph"===n?.type||"text"===n?.type?(n.raw+="\n"+s.raw,n.text+="\n"+s.text,this.inlineQueue.at(-1).src=n.text):t.push(s);continue}if(s=this.tokenizer.fences(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.heading(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.hr(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.blockquote(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.list(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.html(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.def(e)){e=e.substring(s.raw.length);const n=t.at(-1);"paragraph"===n?.type||"text"===n?.type?(n.raw+="\n"+s.raw,n.text+="\n"+s.raw,this.inlineQueue.at(-1).src=n.text):this.tokens.links[s.tag]||(this.tokens.links[s.tag]={href:s.href,title:s.title});continue}if(s=this.tokenizer.table(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.lheading(e)){e=e.substring(s.raw.length),t.push(s);continue}let r=e;if(this.options.extensions?.startBlock){let t=1/0;const n=e.slice(1);let s;this.options.extensions.startBlock.forEach((e=>{s=e.call({lexer:this},n),"number"==typeof s&&s>=0&&(t=Math.min(t,s))})),t<1/0&&t>=0&&(r=e.substring(0,t+1))}if(this.state.top&&(s=this.tokenizer.paragraph(r))){const i=t.at(-1);n&&"paragraph"===i?.type?(i.raw+="\n"+s.raw,i.text+="\n"+s.text,this.inlineQueue.pop(),this.inlineQueue.at(-1).src=i.text):t.push(s),n=r.length!==e.length,e=e.substring(s.raw.length)}else if(s=this.tokenizer.text(e)){e=e.substring(s.raw.length);const n=t.at(-1);"text"===n?.type?(n.raw+="\n"+s.raw,n.text+="\n"+s.text,this.inlineQueue.pop(),this.inlineQueue.at(-1).src=n.text):t.push(s)}else if(e){const t="Infinite loop on byte: "+e.charCodeAt(0);if(this.options.silent){console.error(t);break}throw new Error(t)}}return this.state.top=!0,t}inline(e,t=[]){return this.inlineQueue.push({src:e,tokens:t}),t}inlineTokens(e,t=[]){let n=e,s=null;if(this.tokens.links){const e=Object.keys(this.tokens.links);if(e.length>0)for(;null!=(s=this.tokenizer.rules.inline.reflinkSearch.exec(n));)e.includes(s[0].slice(s[0].lastIndexOf("[")+1,-1))&&(n=n.slice(0,s.index)+"["+"a".repeat(s[0].length-2)+"]"+n.slice(this.tokenizer.rules.inline.reflinkSearch.lastIndex))}for(;null!=(s=this.tokenizer.rules.inline.blockSkip.exec(n));)n=n.slice(0,s.index)+"["+"a".repeat(s[0].length-2)+"]"+n.slice(this.tokenizer.rules.inline.blockSkip.lastIndex);for(;null!=(s=this.tokenizer.rules.inline.anyPunctuation.exec(n));)n=n.slice(0,s.index)+"++"+n.slice(this.tokenizer.rules.inline.anyPunctuation.lastIndex);let r=!1,i="";for(;e;){let s;if(r||(i=""),r=!1,this.options.extensions?.inline?.some((n=>!!(s=n.call({lexer:this},e,t))&&(e=e.substring(s.raw.length),t.push(s),!0))))continue;if(s=this.tokenizer.escape(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.tag(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.link(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.reflink(e,this.tokens.links)){e=e.substring(s.raw.length);const n=t.at(-1);"text"===s.type&&"text"===n?.type?(n.raw+=s.raw,n.text+=s.text):t.push(s);continue}if(s=this.tokenizer.emStrong(e,n,i)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.codespan(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.br(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.del(e)){e=e.substring(s.raw.length),t.push(s);continue}if(s=this.tokenizer.autolink(e)){e=e.substring(s.raw.length),t.push(s);continue}if(!this.state.inLink&&(s=this.tokenizer.url(e))){e=e.substring(s.raw.length),t.push(s);continue}let l=e;if(this.options.extensions?.startInline){let t=1/0;const n=e.slice(1);let s;this.options.extensions.startInline.forEach((e=>{s=e.call({lexer:this},n),"number"==typeof s&&s>=0&&(t=Math.min(t,s))})),t<1/0&&t>=0&&(l=e.substring(0,t+1))}if(s=this.tokenizer.inlineText(l)){e=e.substring(s.raw.length),"_"!==s.raw.slice(-1)&&(i=s.raw.slice(-1)),r=!0;const n=t.at(-1);"text"===n?.type?(n.raw+=s.raw,n.text+=s.text):t.push(s)}else if(e){const t="Infinite loop on byte: "+e.charCodeAt(0);if(this.options.silent){console.error(t);break}throw new Error(t)}}return t}}class ie{options;parser;constructor(t){this.options=t||e.defaults}space(e){return""}code({text:e,lang:t,escaped:n}){const s=(t||"").match(i.notSpaceStart)?.[0],r=e.replace(i.endingNewline,"")+"\n";return s?'<pre><code class="language-'+W(s)+'">'+(n?r:W(r,!0))+"</code></pre>\n":"<pre><code>"+(n?r:W(r,!0))+"</code></pre>\n"}blockquote({tokens:e}){return`<blockquote>\n${this.parser.parse(e)}</blockquote>\n`}html({text:e}){return e}heading({tokens:e,depth:t}){return`<h${t}>${this.parser.parseInline(e)}</h${t}>\n`}hr(e){return"<hr>\n"}list(e){const t=e.ordered,n=e.start;let s="";for(let t=0;t<e.items.length;t++){const n=e.items[t];s+=this.listitem(n)}const r=t?"ol":"ul";return"<"+r+(t&&1!==n?' start="'+n+'"':"")+">\n"+s+"</"+r+">\n"}listitem(e){let t="";if(e.task){const n=this.checkbox({checked:!!e.checked});e.loose?"paragraph"===e.tokens[0]?.type?(e.tokens[0].text=n+" "+e.tokens[0].text,e.tokens[0].tokens&&e.tokens[0].tokens.length>0&&"text"===e.tokens[0].tokens[0].type&&(e.tokens[0].tokens[0].text=n+" "+W(e.tokens[0].tokens[0].text),e.tokens[0].tokens[0].escaped=!0)):e.tokens.unshift({type:"text",raw:n+" ",text:n+" ",escaped:!0}):t+=n+" "}return t+=this.parser.parse(e.tokens,!!e.loose),`<li>${t}</li>\n`}checkbox({checked:e}){return"<input "+(e?'checked="" ':"")+'disabled="" type="checkbox">'}paragraph({tokens:e}){return`<p>${this.parser.parseInline(e)}</p>\n`}table(e){let t="",n="";for(let t=0;t<e.header.length;t++)n+=this.tablecell(e.header[t]);t+=this.tablerow({text:n});let s="";for(let t=0;t<e.rows.length;t++){const r=e.rows[t];n="";for(let e=0;e<r.length;e++)n+=this.tablecell(r[e]);s+=this.tablerow({text:n})}return s&&(s=`<tbody>${s}</tbody>`),"<table>\n<thead>\n"+t+"</thead>\n"+s+"</table>\n"}tablerow({text:e}){return`<tr>\n${e}</tr>\n`}tablecell(e){const t=this.parser.parseInline(e.tokens),n=e.header?"th":"td";return(e.align?`<${n} align="${e.align}">`:`<${n}>`)+t+`</${n}>\n`}strong({tokens:e}){return`<strong>${this.parser.parseInline(e)}</strong>`}em({tokens:e}){return`<em>${this.parser.parseInline(e)}</em>`}codespan({text:e}){return`<code>${W(e,!0)}</code>`}br(e){return"<br>"}del({tokens:e}){return`<del>${this.parser.parseInline(e)}</del>`}link({href:e,title:t,tokens:n}){const s=this.parser.parseInline(n),r=Y(e);if(null===r)return s;let i='<a href="'+(e=r)+'"';return t&&(i+=' title="'+W(t)+'"'),i+=">"+s+"</a>",i}image({href:e,title:t,text:n}){const s=Y(e);if(null===s)return W(n);let r=`<img src="${e=s}" alt="${n}"`;return t&&(r+=` title="${W(t)}"`),r+=">",r}text(e){return"tokens"in e&&e.tokens?this.parser.parseInline(e.tokens):"escaped"in e&&e.escaped?e.text:W(e.text)}}class le{strong({text:e}){return e}em({text:e}){return e}codespan({text:e}){return e}del({text:e}){return e}html({text:e}){return e}text({text:e}){return e}link({text:e}){return""+e}image({text:e}){return""+e}br(){return""}}class oe{options;renderer;textRenderer;constructor(t){this.options=t||e.defaults,this.options.renderer=this.options.renderer||new ie,this.renderer=this.options.renderer,this.renderer.options=this.options,this.renderer.parser=this,this.textRenderer=new le}static parse(e,t){return new oe(t).parse(e)}static parseInline(e,t){return new oe(t).parseInline(e)}parse(e,t=!0){let n="";for(let s=0;s<e.length;s++){const r=e[s];if(this.options.extensions?.renderers?.[r.type]){const e=r,t=this.options.extensions.renderers[e.type].call({parser:this},e);if(!1!==t||!["space","hr","heading","code","table","blockquote","list","html","paragraph","text"].includes(e.type)){n+=t||"";continue}}const i=r;switch(i.type){case"space":n+=this.renderer.space(i);continue;case"hr":n+=this.renderer.hr(i);continue;case"heading":n+=this.renderer.heading(i);continue;case"code":n+=this.renderer.code(i);continue;case"table":n+=this.renderer.table(i);continue;case"blockquote":n+=this.renderer.blockquote(i);continue;case"list":n+=this.renderer.list(i);continue;case"html":n+=this.renderer.html(i);continue;case"paragraph":n+=this.renderer.paragraph(i);continue;case"text":{let r=i,l=this.renderer.text(r);for(;s+1<e.length&&"text"===e[s+1].type;)r=e[++s],l+="\n"+this.renderer.text(r);n+=t?this.renderer.paragraph({type:"paragraph",raw:l,text:l,tokens:[{type:"text",raw:l,text:l,escaped:!0}]}):l;continue}default:{const e='Token with "'+i.type+'" type was not found.';if(this.options.silent)return console.error(e),"";throw new Error(e)}}}return n}parseInline(e,t=this.renderer){let n="";for(let s=0;s<e.length;s++){const r=e[s];if(this.options.extensions?.renderers?.[r.type]){const e=this.options.extensions.renderers[r.type].call({parser:this},r);if(!1!==e||!["escape","html","link","image","strong","em","codespan","br","del","text"].includes(r.type)){n+=e||"";continue}}const i=r;switch(i.type){case"escape":case"text":n+=t.text(i);break;case"html":n+=t.html(i);break;case"link":n+=t.link(i);break;case"image":n+=t.image(i);break;case"strong":n+=t.strong(i);break;case"em":n+=t.em(i);break;case"codespan":n+=t.codespan(i);break;case"br":n+=t.br(i);break;case"del":n+=t.del(i);break;default:{const e='Token with "'+i.type+'" type was not found.';if(this.options.silent)return console.error(e),"";throw new Error(e)}}}return n}}class ae{options;block;constructor(t){this.options=t||e.defaults}static passThroughHooks=new Set(["preprocess","postprocess","processAllTokens"]);preprocess(e){return e}postprocess(e){return e}processAllTokens(e){return e}provideLexer(){return this.block?re.lex:re.lexInline}provideParser(){return this.block?oe.parse:oe.parseInline}}class ce{defaults={async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null};options=this.setOptions;parse=this.parseMarkdown(!0);parseInline=this.parseMarkdown(!1);Parser=oe;Renderer=ie;TextRenderer=le;Lexer=re;Tokenizer=se;Hooks=ae;constructor(...e){this.use(...e)}walkTokens(e,t){let n=[];for(const s of e)switch(n=n.concat(t.call(this,s)),s.type){case"table":{const e=s;for(const s of e.header)n=n.concat(this.walkTokens(s.tokens,t));for(const s of e.rows)for(const e of s)n=n.concat(this.walkTokens(e.tokens,t));break}case"list":{const e=s;n=n.concat(this.walkTokens(e.items,t));break}default:{const e=s;this.defaults.extensions?.childTokens?.[e.type]?this.defaults.extensions.childTokens[e.type].forEach((s=>{const r=e[s].flat(1/0);n=n.concat(this.walkTokens(r,t))})):e.tokens&&(n=n.concat(this.walkTokens(e.tokens,t)))}}return n}use(...e){const t=this.defaults.extensions||{renderers:{},childTokens:{}};return e.forEach((e=>{const n={...e};if(n.async=this.defaults.async||n.async||!1,e.extensions&&(e.extensions.forEach((e=>{if(!e.name)throw new Error("extension name required");if("renderer"in e){const n=t.renderers[e.name];t.renderers[e.name]=n?function(...t){let s=e.renderer.apply(this,t);return!1===s&&(s=n.apply(this,t)),s}:e.renderer}if("tokenizer"in e){if(!e.level||"block"!==e.level&&"inline"!==e.level)throw new Error("extension level must be 'block' or 'inline'");const n=t[e.level];n?n.unshift(e.tokenizer):t[e.level]=[e.tokenizer],e.start&&("block"===e.level?t.startBlock?t.startBlock.push(e.start):t.startBlock=[e.start]:"inline"===e.level&&(t.startInline?t.startInline.push(e.start):t.startInline=[e.start]))}"childTokens"in e&&e.childTokens&&(t.childTokens[e.name]=e.childTokens)})),n.extensions=t),e.renderer){const t=this.defaults.renderer||new ie(this.defaults);for(const n in e.renderer){if(!(n in t))throw new Error(`renderer '${n}' does not exist`);if(["options","parser"].includes(n))continue;const s=n,r=e.renderer[s],i=t[s];t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n||""}}n.renderer=t}if(e.tokenizer){const t=this.defaults.tokenizer||new se(this.defaults);for(const n in e.tokenizer){if(!(n in t))throw new Error(`tokenizer '${n}' does not exist`);if(["options","rules","lexer"].includes(n))continue;const s=n,r=e.tokenizer[s],i=t[s];t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n}}n.tokenizer=t}if(e.hooks){const t=this.defaults.hooks||new ae;for(const n in e.hooks){if(!(n in t))throw new Error(`hook '${n}' does not exist`);if(["options","block"].includes(n))continue;const s=n,r=e.hooks[s],i=t[s];ae.passThroughHooks.has(n)?t[s]=e=>{if(this.defaults.async)return Promise.resolve(r.call(t,e)).then((e=>i.call(t,e)));const n=r.call(t,e);return i.call(t,n)}:t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n}}n.hooks=t}if(e.walkTokens){const t=this.defaults.walkTokens,s=e.walkTokens;n.walkTokens=function(e){let n=[];return n.push(s.call(this,e)),t&&(n=n.concat(t.call(this,e))),n}}this.defaults={...this.defaults,...n}})),this}setOptions(e){return this.defaults={...this.defaults,...e},this}lexer(e,t){return re.lex(e,t??this.defaults)}parser(e,t){return oe.parse(e,t??this.defaults)}parseMarkdown(e){return(t,n)=>{const s={...n},r={...this.defaults,...s},i=this.onError(!!r.silent,!!r.async);if(!0===this.defaults.async&&!1===s.async)return i(new Error("marked(): The async option was set to true by an extension. Remove async: false from the parse options object to return a Promise."));if(null==t)return i(new Error("marked(): input parameter is undefined or null"));if("string"!=typeof t)return i(new Error("marked(): input parameter is of type "+Object.prototype.toString.call(t)+", string expected"));r.hooks&&(r.hooks.options=r,r.hooks.block=e);const l=r.hooks?r.hooks.provideLexer():e?re.lex:re.lexInline,o=r.hooks?r.hooks.provideParser():e?oe.parse:oe.parseInline;if(r.async)return Promise.resolve(r.hooks?r.hooks.preprocess(t):t).then((e=>l(e,r))).then((e=>r.hooks?r.hooks.processAllTokens(e):e)).then((e=>r.walkTokens?Promise.all(this.walkTokens(e,r.walkTokens)).then((()=>e)):e)).then((e=>o(e,r))).then((e=>r.hooks?r.hooks.postprocess(e):e)).catch(i);try{r.hooks&&(t=r.hooks.preprocess(t));let e=l(t,r);r.hooks&&(e=r.hooks.processAllTokens(e)),r.walkTokens&&this.walkTokens(e,r.walkTokens);let n=o(e,r);return r.hooks&&(n=r.hooks.postprocess(n)),n}catch(e){return i(e)}}}onError(e,t){return n=>{if(n.message+="\nPlease report this to https://github.com/markedjs/marked.",e){const e="<p>An error occurred:</p><pre>"+W(n.message+"",!0)+"</pre>";return t?Promise.resolve(e):e}if(t)return Promise.reject(n);throw n}}}const he=new ce;function pe(e,t){return he.parse(e,t)}pe.options=pe.setOptions=function(e){return he.setOptions(e),pe.defaults=he.defaults,n(pe.defaults),pe},pe.getDefaults=t,pe.defaults=e.defaults,pe.use=function(...e){return he.use(...e),pe.defaults=he.defaults,n(pe.defaults),pe},pe.walkTokens=function(e,t){return he.walkTokens(e,t)},pe.parseInline=he.parseInline,pe.Parser=oe,pe.parser=oe.parse,pe.Renderer=ie,pe.TextRenderer=le,pe.Lexer=re,pe.lexer=re.lex,pe.Tokenizer=se,pe.Hooks=ae,pe.parse=pe;const ue=pe.options,ge=pe.setOptions,ke=pe.use,de=pe.walkTokens,fe=pe.parseInline,xe=pe,be=oe.parse,we=re.lex;e.Hooks=ae,e.Lexer=re,e.Marked=ce,e.Parser=oe,e.Renderer=ie,e.TextRenderer=le,e.Tokenizer=se,e.getDefaults=t,e.lexer=we,e.marked=pe,e.options=ue,e.parse=xe,e.parseInline=fe,e.parser=be,e.setOptions=ge,e.use=ke,e.walkTokens=de}));

</script>
<script>
marked.setOptions({ gfm: true, breaks: true });
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
    return `<div class="block-text">${marked.parse(b.text)}</div>`;

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
      <div class="msg-text">${marked.parse(e.text)}</div></div>`;

  const blocks = (e.blocks||[]).map(renderBlock).join('');
  if (e.type === 'tool_response')
    return `<div class="entry entry-tool-response">${blocks}</div>`;
  if (e.role === 'user')
    return `<div class="entry entry-user"><div class="role-label">user ${ts}</div>${blocks}</div>`;
  if (e.role === 'assistant')
    return `<div class="entry entry-assistant"><div class="role-label">claude ${ts}</div>${blocks}</div>`;
  return `<div class="entry">${blocks}</div>`;
}

let pollTimer = null;
async function poll() {
  try {
    const r = await fetch(`/api/entries?after=${cursor}`);
    const d = await r.json();
    if (d.gone) {
      const dot = document.querySelector('.dot');
      const status = document.querySelector('.status');
      if (dot) dot.style.background = 'var(--danger)';
      if (dot) dot.style.animation = 'none';
      if (status) { status.style.color = 'var(--danger)'; status.innerHTML = '<span class="dot" style="background:var(--danger);animation:none"></span> session ended'; }
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      return;
    }
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

bindSlider('fontSize', '--font-size', 'px', 'fontSizeVal');
bindSlider('codeSize', '--code-size', 'px', 'codeSizeVal');

const themes = {
  dark: {
    '--bg':'#0f1117','--bg-card':'#181a24','--bg-tool':'#1c1f30',
    '--bg-result':'#161924','--bg-thinking':'#1c1a28',
    '--accent':'#7c5cfc','--accent-dim':'rgba(124,92,252,0.15)',
    '--text':'#e2e8f0','--text-muted':'#b8c4d4','--text-dim':'#8494a7',
    '--text-code':'#c8d5e4','--border':'rgba(255,255,255,0.06)',
  },
  mid: {
    '--bg':'#1e2030','--bg-card':'#262840','--bg-tool':'#2a2d48',
    '--bg-result':'#232538','--bg-thinking':'#282a42',
    '--accent':'#8b6cff','--accent-dim':'rgba(139,108,255,0.15)',
    '--text':'#d4dce8','--text-muted':'#a8b4c4','--text-dim':'#7a8a9c',
    '--text-code':'#bcc8d8','--border':'rgba(255,255,255,0.08)',
  },
  light: {
    '--bg':'#f5f5f5','--bg-card':'#ffffff','--bg-tool':'#f0f0f4',
    '--bg-result':'#fafafa','--bg-thinking':'#f4f2fb',
    '--accent':'#6b4fd8','--accent-dim':'rgba(107,79,216,0.1)',
    '--text':'#1a1a2e','--text-muted':'#4a4a6a','--text-dim':'#7a7a9a',
    '--text-code':'#2d2d4e','--border':'rgba(0,0,0,0.08)',
  },
};
let currentTheme = 'dark';

function applyTheme(name) {
  const t = themes[name];
  if (!t) return;
  currentTheme = name;
  for (const [k, v] of Object.entries(t)) root.style.setProperty(k, v);
  document.querySelectorAll('.theme-btn').forEach(b => {
    b.style.borderColor = b.dataset.theme === name ? 'var(--accent)' : '';
    b.style.fontWeight = b.dataset.theme === name ? '700' : '';
  });
  saveSettings();
}

document.querySelectorAll('.theme-btn').forEach(btn => {
  btn.onclick = () => applyTheme(btn.dataset.theme);
});

function saveSettings() {
  const s = {
    fontSize: parseFloat($('fontSize').value),
    codeSize: parseFloat($('codeSize').value),
    theme: currentTheme,
  };
  localStorage.setItem('claude-live-settings', JSON.stringify(s));
}
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('claude-live-settings'));
    if (s) {
      if (s.theme) applyTheme(s.theme);
      if (s.fontSize) {
        root.style.setProperty('--font-size', s.fontSize + 'px');
        $('fontSize').value = s.fontSize;
        $('fontSizeVal').textContent = s.fontSize + 'px';
      }
      if (s.codeSize) {
        root.style.setProperty('--code-size', s.codeSize + 'px');
        $('codeSize').value = s.codeSize;
        $('codeSizeVal').textContent = s.codeSize + 'px';
      }
    }
  } catch {}
}
loadSettings();

// Load file info
fetch('/api/info').then(r=>r.json()).then(d => {
  statFile.textContent = d.file;
});

poll();
pollTimer = setInterval(poll, __POLL_INTERVAL_MS__);
</script>
</body>
</html>
"""


# ── CLI Entry Point ────────────────────────────────────────

def _get_lan_ip():
    """Best-effort LAN IP via UDP socket (no traffic sent)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None


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
    parser.add_argument(
        "--interval", type=float, default=1.5,
        help="Poll interval in seconds (default: 1.5)"
    )
    args = parser.parse_args()

    if args.session:
        session_path = Path(args.session).resolve()
        source = "provided"
    else:
        session_path = pick_session()
        source = "auto-detected"

    if not session_path or not session_path.exists():
        print("Error: no session file found.", file=sys.stderr)
        print("Usage: claude-code-live [path/to/session.jsonl]", file=sys.stderr)
        sys.exit(1)

    print(f"  Session:  {session_path} ({source})")
    print(f"  Size:     {session_path.stat().st_size:,} bytes")

    LiveHandler.session_path = str(session_path)
    LiveHandler.poll_interval_ms = int(args.interval * 1000)
    port = args.port
    server = None
    for attempt in range(10):
        try:
            server = HTTPServer(("0.0.0.0", port), LiveHandler)
            break
        except OSError as e:
            if e.errno in (98, 48, 10048):  # Linux, macOS, Windows
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
    print(f"  Local:    http://localhost:{port}")
    lan_ip = _get_lan_ip()
    if lan_ip:
        print(f"  Network:  http://{lan_ip}:{port}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
