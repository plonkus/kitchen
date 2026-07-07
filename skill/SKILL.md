---
name: claude-kitchen
description: Use when setting up, managing, or orchestrating multi-agent workflows with the kitchen CLI. Also use when asked to hire cooks, check brigade status, or send tickets.
---

# claude-kitchen

Multi-agent orchestration using tmux + Claude Code channels. A **Sous Chef** (you, if running in a kitchen) coordinates **Cook** agents in parallel.

## Are you the sous chef?

If `$AGENT_SESSION` is set, you ARE the sous chef in an active kitchen. Do NOT call `kitchen open` — you're already in one. Just hire cooks and send tickets.

## CLI Reference

| Command | What it does |
|---------|-------------|
| `kitchen open [name] [project]` | Start a kitchen (human runs this, not the sous) |
| `kitchen hire <name> --backend claude\|codex [--role ROLE] [--effort LEVEL]` | Spawn a cook in the kitchen |
| `kitchen roles` | List available cook roles |
| `kitchen ticket <cook> "message"` | Send a task to a cook |
| `kitchen peek <cook> [--full]` | Capture a cook's screen |
| `kitchen brigade` | Status of all cooks |
| `kitchen clock-out <cook>` | Hard-kill a cook |
| `kitchen close` | Shut down the kitchen |
| `kitchen setup` | Check hooks, skill, and dependencies are installed |

## Sous Chef Workflow

```bash
# Hire cooks
kitchen hire eng --backend claude --role eng
kitchen hire reviewer --backend codex

# Send work
kitchen ticket eng "fix the auth bug in src/auth.py"
kitchen ticket reviewer "review the PR"

# Check on cooks
kitchen brigade
kitchen peek eng

# When done
kitchen clock-out eng
```

## How notifications work

When a cook finishes, you'll see a `← kitchen:` message with the cook's output. This comes through Claude Code's channels feature — no polling, no manual checking needed. Just respond to notifications as they arrive.

## Key details

- **`--backend claude|codex`** — cooks can be Claude Code or Codex
- **Cook role** — `kitchen hire eng --role <role>` loads a role prompt at boot. Claude cooks get it via `--append-system-prompt-file`; Codex cooks get it as a first message after the prompt is ready. Both backends support `--role`. Run `kitchen roles` to list available roles. Cooks default to `_default` (a generic "wait for a ticket" prompt).
- **Sending work** — after `kitchen hire`, the sous fires the first ticket via `kitchen ticket <cook> "..."`. There is no `--task` flag (was removed in favor of explicit ticketing).
- **`--effort` flag** — reasoning effort: `low`, `medium`, `high` (default), `max`. Only use this if the human explicitly requests a specific effort level. Do not set it on your own.
- **State lives in** `~/.claude-kitchen/<project-slug>-<name>/` — cook status JSON files
- **tmux sessions** named `ck-<project-slug>-<name>` (e.g., `ck-my-project-risotto`). The project slug is the repo name, so the same name in two repos never collides. Human can `tmux attach` to observe cooks.
