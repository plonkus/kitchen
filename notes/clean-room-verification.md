# `--clean-room` cook — isolation verification

Durable evidence for `kitchen hire <name> --clean-room` (Claude-only eval/isolation
hire). Captured empirically on **2026-06-24**, Claude Code **2.1.190**, on the user's
normal **subscription/OAuth** login (no `ANTHROPIC_API_KEY`, no `--bare`/`--safe-mode`).

## What `--clean-room` excludes (Claude cook)

| Layer | Mechanism | In scope for v2? |
|-------|-----------|------------------|
| Auto-memory (`MEMORY.md` + memory files) | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` env var | ✅ disabled |
| superpowers plugin (SessionStart "You have superpowers" injection) | `--settings '{"enabledPlugins":{"superpowers@superpowers-marketplace":false}}'` | ✅ disabled |
| Role prompt (`roles/_default.md` etc.) | no `--append-system-prompt-file` passed | ✅ omitted — sous sends the eval prompt via a ticket |
| `CLAUDE.md` (project + user) | — | ❌ still loads (left as a clean seam for a later pass) |
| Skill availability | — | ❌ skills still resolvable (only superpowers' *injection* is gone) |

Plugin key confirmed against `~/.claude/settings.json` `enabledPlugins`:
`superpowers@superpowers-marketplace`.

## Exact launch command `build_shell_cmd(..., clean_room=True)` emits

```
bash -lc 'export AGENT_NAME=eval1 AGENT_SESSION=ck-claude-kitchen STATUS_DIR=/Users/plucas/.claude-kitchen/kitchen-claude-kitchen KITCHEN_NOTES=... KITCHEN_WIKI=...; CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 exec claude --dangerously-skip-permissions --disallowedTools AskUserQuestion --settings '{"enabledPlugins":{"superpowers@superpowers-marketplace":false}}''
```

Note: the auto-memory env var is a temp assignment **before** `exec`; the plugin
disable is a `--settings` arg **after** it; and there is **no** `--append-system-prompt-file`.

## Working directory — sous-managed, NOT part of `--clean-room`

`--clean-room` does only the two knobs + no-role above. The cook's working directory
is the real lever for project-local context (CLAUDE.md discovery walks up from cwd;
auto-memory is keyed by the cwd project path), and it is set with the **existing
`--project <dir>`** arg on `kitchen hire` — no new flag was added. `resolve_project`
requires an existing directory; the cook's tmux window is opened with that cwd.

Exact command the sous runs to hire a clean-room cook in a chosen directory:

```
kitchen hire eval1 --clean-room --project /abs/path/to/eval-dir
```

For a true clean room, point `--project` at an empty dir or a pinned checkout (no
`CLAUDE.md`). Omitting `--project` defaults to the sous's cwd.

## Method

For each variant, run `claude -p` with the cook's exact launch env (incl.
`AGENT_NAME`/`AGENT_SESSION`/`STATUS_DIR`) plus a 4-line self-report probe asking
whether the opening context contains: the superpowers SessionStart block (`SP`),
project `CLAUDE.md` (`CLAUDEMD`), auto-memory (`MEM`), and a skills list (`SKILL`).
`STATUS_DIR` points at a throwaway dir so the live kitchen is untouched; the Stop
hook then writes the cook's completion status there — proving it fired.
(`YES` = layer present/leaking.)

## Results — paired capture

| Probe | Normal cook | `--clean-room` cook |
|-------|-------------|---------------------|
| `SP` (superpowers injection) | **YES** | **NO** ✅ |
| `MEM` (auto-memory) | **YES** | **NO** ✅ |
| `CLAUDEMD` | YES | YES (out of scope) |
| `SKILL` | YES | YES (out of scope) |

## Critical check — `--settings` MERGES, Stop hook stays alive

The kitchen's own `Stop` hook lives in `~/.claude/settings.json`. Passing `--settings`
to disable the plugin must merge with that file, not replace it — otherwise the cook
could no longer notify the sous on completion. Both clean-room and normal cooks wrote
their completion status via `kitchen hook` (the Stop hook), confirming the merge:

```
# clean-room cook — Stop hook fired (superpowers disabled via --settings):
{"agent": "cleanroom", "session": "ck-claude-kitchen", "status": "idle",
 "summary": "SP=NO\nCLAUDEMD=YES\nMEM=NO\nSKILL=YES", "session_id": "3fa14bcf-...",
 "tokens": {"input": 21609, "max": 1000000}}

# normal cook — Stop hook fired:
{"agent": "normal", "session": "ck-claude-kitchen", "status": "idle",
 "summary": "SP=YES\nCLAUDEMD=YES\nMEM=YES\nSKILL=YES", "session_id": "3927aa33-..."}
```

A written `cooks/<name>.json` is only produced when `kitchen hook` runs to completion,
so its presence is direct proof the Stop hook fired under clean-room. Auth also held
(the session completed with no API key), confirming subscription/OAuth is unaffected.

## Backend handling

`--clean-room --backend codex` and `--clean-room --backend gemini` fail loud:
`--clean-room is only supported for Claude cooks, not '<backend>' (not yet implemented).`
