# claude-kitchen

Multi-agent orchestration over tmux. A sous chef — always Claude — runs in the user's terminal, dispatching cooks (Claude, Codex or Gemini) in tmux windows; cook hooks push notifications through a unix socket into the sous's context as `<channel>` tags.

- `channel.py` — MCP server + unix socket listener, one per kitchen
- `cli.py` — every command: open, hire, ticket, peek, brigade, clock-out, close, setup, hooks
- `spawn.py` — tmux window/process creation for cooks and sous
- `state.py` — cook status JSON read/write
- `tmux.py` — tmux helpers (send-keys, capture-pane)

## Code style

Low code, clean code, DRY — keep it small; one function rather than three. No fallback logic, no defensive complexity: this is a dev tool, so if setup is wrong, fail clearly. (One carveout: `project_slug` falls back from `remote.origin.url` to a slugified toplevel path, so local-only repos still work.) Python is managed by `uv`; install with `uv tool install --editable .`

## Hooks

The hook event type comes from stdin JSON `hook_event_name`, not env vars. The sous hook is a no-op — its body early-returns when `AGENT_NAME=sous` — which prevents echo loops.
