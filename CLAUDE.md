# claude-kitchen

Multi-agent orchestration over tmux. A sous chef — always Claude — runs in the user's terminal, dispatching cooks (Claude, Codex or Gemini) in tmux windows; cook hooks push notifications through a unix socket into the sous's context as `<channel>` tags.

## Code style

Low code, clean code, DRY — keep it small; one function rather than three. No fallback logic, no defensive complexity: this is a dev tool, so if setup is wrong, fail clearly. (One carveout: `project_slug` falls back from `remote.origin.url` to a slugified toplevel path, so local-only repos still work.) Python is managed by `uv`; install with `uv tool install --editable .`

## Hooks

The hook event type comes from stdin JSON `hook_event_name`, not env vars. The sous hook is a no-op — its body early-returns when `AGENT_NAME=sous` — which prevents echo loops.

## Gotchas

These are the things that cost someone an afternoon. None are discoverable by reading the file tree.

- **`server:kitchen · no MCP server configured` at startup is harmless.** A known race — the server connects fine a moment later. Do not "fix" it.
- **Prompt injection differs by backend.** `sous-chef.md` reaches the sous via `--append-system-prompt` on `kitchen open`. Claude cooks get their role via `--append-system-prompt-file` at launch; Codex has no equivalent flag, so Codex cooks receive the role through `send_keys` as the first message after `wait_for_prompt` succeeds. A role that never arrives on a Codex cook is almost always a `wait_for_prompt` timeout, not a missing file.
- **`AGENT_KITCHEN` carries kitchen identity, not `AGENT_SESSION`.** The tmux session name is the constant `kitchen` now, so every consumer — `resolve_kitchen`, the statusline, the hook gate — reads `AGENT_KITCHEN`. A cook missing it is mute: `_hook_gate()` returns `None` and every hook silently no-ops.
