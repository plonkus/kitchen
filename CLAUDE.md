# claude-kitchen

Multi-agent orchestration using tmux + Claude Code channels.

## Architecture

Sous chef runs Claude in the user's terminal with `--dangerously-load-development-channels`. A per-kitchen MCP channel server (`channel.py`) listens on a unix domain socket. Cook hooks send notifications through the socket, which the MCP server pushes into the sous's context as `<channel>` tags.

- `channel.py` — MCP server + unix socket listener (one per kitchen)
- `cli.py` — all commands: open, hire, ticket, peek, brigade, clock-out, close, setup, hooks
- `spawn.py` — tmux window/process creation for cooks and sous
- `state.py` — cook status JSON read/write
- `tmux.py` — tmux helpers (send-keys, capture-pane, etc.)

## Code style

- Low code, clean code, DRY. Keep the codebase small.
- No fallback logic, no defensive complexity. It's a dev tool — if setup is wrong, fail. (One carveout: `project_slug` falls back from `remote.origin.url` to a slugified toplevel path, so throwaway local-only repos still work.)
- Prefer failing clearly over handling edge cases gracefully.
- If something can be one function instead of three, make it one.
- Python managed via `uv`. Install with `uv tool install --editable .`

## Key details

- Sous is always Claude. Cooks can be Claude or Codex.
- Hook event type comes from stdin JSON `hook_event_name`, not env vars.
- Sous hook is a no-op (the hook body early-returns when `AGENT_NAME=sous`) to prevent echo loops.
- `sous-chef.md` (in `src/claude_kitchen/`) is injected via `--append-system-prompt` on `kitchen open`.
- Cooks get role prompts from `src/claude_kitchen/roles/<role>.md`. Claude cooks receive the role via `--append-system-prompt-file` at launch. Codex cooks (which have no equivalent flag) receive the role via `send_keys` as the first message after `wait_for_prompt` succeeds. Every cook — Claude or Codex — gets `_default.md` when `--role` is omitted; that prompt tells the cook to wait for a ticket.
- `kitchen setup` checks: Claude hooks, Codex hooks, skill symlink, mcp SDK, superpowers plugin, Claude CLI version (≥ 2.1.80), statusline (advisory — soft-warn, doesn't block), legacy `projects` kitchen collision. Exits non-zero on blocker failure.
- `server:kitchen · no MCP server configured` warning at startup is a known race condition — harmless, server connects fine.
