# claude-live

A real-time web viewer for Claude Code session transcripts.

<!-- TODO: Add screenshot -->

## Why

Claude Code's CLI collapses tool calls — Read, Bash, Edit, and others — behind expandable summaries. When Claude is doing complex multi-step work (rendering screenshots with Playwright, editing files across a codebase, running static analysis), what you actually see is:

```
Read 8 files (ctrl+o to expand)
```

...instead of what actually happened.

**claude-live** gives you a live, scrollable web view of the full session transcript as it happens. Every tool call, every result, every thinking block — laid out in a readable timeline that updates in real time.

Think of it as [Simon Willison's claude-code-transcripts](https://github.com/simonw/claude-code-transcripts), but live.

## Features

- **Live polling** — checks for new entries every 1.5 seconds
- **Subagent inlining** — Task/subagent activity is stitched into the timeline as expandable blocks, so you see what happened inside delegated work
- **Toggle visibility** — independently show/hide thinking blocks, tool results, and subagents
- **Style picker** — adjust font sizes, text colors, and choose from presets (Default, Bright, Large, Compact); settings saved to localStorage
- **Auto-scroll** — follows new entries as they arrive, toggleable
- **Zero dependencies** — Python 3.10+ stdlib only
- **Single file** — the entire server, parser, and UI live in `claude_live.py`

## Quick start

Auto-find the latest session and serve it:

```bash
python3 claude_live.py
```

Or point it at a specific session file:

```bash
python3 claude_live.py /path/to/session.jsonl --port 7777
```

Then open `http://localhost:7777` in your browser.

## How it works

`claude_live.py` is a Python HTTP server that:

1. Reads a Claude Code `.jsonl` session file and parses each line into structured entries (messages, tool calls, tool results, thinking blocks)
2. Discovers subagent transcripts in the session's `subagents/` directory and inlines them at the point they were invoked
3. Serves an embedded HTML/CSS/JS page as the viewer
4. Exposes `/api/entries?after=N` — the page polls this endpoint every 1.5 seconds for new lines, so the view updates live as the session progresses

No build step, no node_modules, no pip install. Just Python and a browser.

## Acknowledgments

Inspired by Simon Willison's [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — a tool for saving and sharing Claude Code transcripts as static HTML. claude-live takes a different angle: watch the session unfold in real time instead of reviewing it after the fact.

## License

[MIT](LICENSE)
