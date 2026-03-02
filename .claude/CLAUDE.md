# claude-code-live

Live web viewer for Claude Code session transcripts.

## What this is

A lightweight Python HTTP server that tails a Claude Code `.jsonl` session file and renders it as a live-updating web UI. Like Simon Willison's `claude-code-transcripts` but real-time instead of post-hoc.

## Architecture

- **Single Python file** (`claude_live.py`) — HTTP server + JSONL parser + embedded HTML/CSS/JS
- **No dependencies** beyond Python 3.10+ stdlib
- **No hardcoded paths** — session file path is a runtime argument
- **Generic** — reads any Claude Code JSONL transcript format

## Project type

Pure Python, no build step. Single-file tool.

## Conventions

- Zero external dependencies — stdlib only (http.server, json, pathlib)
- All HTML/CSS/JS is embedded in the Python file as a string
- The JSONL format comes from Claude Code — we parse what exists, we don't invent fields
- No secrets, no personal data, no machine-specific paths in the source

## File structure

```
claude_live.py    — the entire tool
README.md         — usage docs
```

## Testing

```bash
python3 claude_live.py /path/to/session.jsonl --port 7777
```

Open `http://localhost:7777` to see the live transcript.
