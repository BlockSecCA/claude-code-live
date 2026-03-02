# claude-code-live

A real-time web viewer for Claude Code session transcripts.

<!-- TODO: Add screenshot -->

## Why

Claude Code's CLI collapses tool calls — Read, Bash, Edit, and others — behind expandable summaries. When Claude is doing complex multi-step work (rendering screenshots with Playwright, editing files across a codebase, running static analysis), what you actually see is:

```
Read 8 files (ctrl+o to expand)
```

...instead of what actually happened.

**claude-code-live** gives you a live, scrollable web view of the full session transcript as it happens. Every tool call, every result, every thinking block — laid out in a readable timeline that updates in real time.

Think of it as [Simon Willison's claude-code-transcripts](https://github.com/simonw/claude-code-transcripts), but live.

## Features

- **Live polling** — checks for new entries every 1.5 seconds
- **Subagent inlining** — Task/subagent activity is stitched into the timeline as expandable blocks, so you see what happened inside delegated work
- **Toggle visibility** — independently show/hide thinking blocks, tool results, and subagents
- **Style picker** — adjust font sizes, text colors, and choose from presets (Default, Bright, Large, Compact); settings saved to localStorage
- **Auto-scroll** — follows new entries as they arrive, toggleable
- **Zero dependencies** — Python 3.10+ stdlib only
- **Single file** — the entire server, parser, and UI live in `claude_live.py`

## Install

```bash
pipx install claude-code-live
```

Or install from a local clone:

```bash
git clone https://github.com/carlosplanchon/claude-code-live.git
pipx install ./claude-code-live
```

No dependencies beyond Python 3.10+.

## Usage

**Watch a live session** — run this while Claude Code is active in another terminal:

```bash
claude-code-live
```

With no arguments, claude-code-live auto-detects the most recent session file under `~/.claude/projects/`. If Claude Code is running, that's your current session — the viewer updates in real time as new entries appear.

**Review a past session** — point it at any `.jsonl` transcript:

```bash
claude-code-live /path/to/session.jsonl
```

The behavior is the same either way: serve the file and poll for new lines. A live session keeps growing; a finished one just renders what's there.

**Options:**

```
claude-code-live [session.jsonl] [--port PORT]
```

Default port is `7777`. If that port is in use (e.g. another instance is already running), it will automatically try the next available port. The server binds to `0.0.0.0`, so on a remote/headless machine (common with Claude Code), open `http://<machine-ip>:<port>` from any browser on your network.

## How it works

`claude_live.py` is a single Python file — HTTP server, JSONL parser, and embedded HTML/CSS/JS viewer. It:

1. Reads a Claude Code `.jsonl` session file and parses each line into structured entries (messages, tool calls, tool results, thinking blocks)
2. Discovers subagent transcripts in the session's `subagents/` directory and inlines them at the point they were invoked
3. Serves the viewer page and exposes `/api/entries?after=N` — the page polls this endpoint every 1.5 seconds for new lines

## Caveat

Claude Code's `.jsonl` transcript format and storage location (`~/.claude/projects/`) are not officially documented by Anthropic. This tool is based on reverse-engineering the current format. If Anthropic changes the log structure or location, claude-code-live may break until updated.

## Acknowledgments

Inspired by Simon Willison's [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — a tool for saving and sharing Claude Code transcripts as static HTML. claude-code-live takes a different angle: watch the session unfold in real time instead of reviewing it after the fact.

## License

[MIT](LICENSE)
