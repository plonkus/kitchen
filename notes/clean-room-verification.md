# `--clean-room` cook — isolation verification

Durable evidence for `kitchen hire <name> --clean-room`. Claude evidence captured
**2026-06-24** (Claude Code **2.1.190**); Codex evidence captured **2026-06-26**
(codex-cli **0.142.2**). Both on the user's normal **subscription/OAuth** login
(no `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env, no `--bare`/`--safe-mode`).

> Codex support: see the "Codex clean-room" section at the bottom. Claude sections below.

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

`--clean-room` supports claude (above) and codex (see the Codex section below).
`--clean-room --backend gemini` fails loud:
`--clean-room is only supported for Claude and Codex cooks, not 'gemini' (not yet implemented).`

---

# Codex clean-room (Option A) — verification

`kitchen hire <name> --clean-room --backend codex` keeps the interactive-codex cook
model and gives each cook a **fresh per-cook `CODEX_HOME`** (`<kitchen-base>/codex-home/<name>`)
seeded with **only `auth.json`** + a minimal `config.toml` granting trust to cwd. Captured
**2026-06-26**, codex-cli **0.142.2**, subscription auth (no `OPENAI_API_KEY` env).

## Mapping the 3 disables to Codex

| Layer | Mechanism |
|-------|-----------|
| Memory | Fresh `CODEX_HOME` has no `~/.codex/memories/` (real `MEMORY.md` not copied); the `memories` feature is `experimental`/off by default. |
| Plugin/skill startup injection | Fresh `CODEX_HOME` has no enabled plugin registry → no SessionStart-codex injection (superpowers isn't an enabled codex plugin anyway). Achieved by OMITTING the registry, NOT by touching `[features].hooks`. |
| Role prompt | `cmd_hire` passes no role; the codex role `send_keys` is guarded `if backend == "codex" and role_path:` so it's skipped. Sous tickets the eval prompt. |

## Auth + notify preserved

- **Auth:** seed ONLY `auth.json` (keys: `auth_mode`, `OPENAI_API_KEY`, `tokens`, `last_refresh` — pure credentials). Cook booted authenticated (gpt-5.5, "2 usage limit resets available").
- **Notify:** `-c notify=["kitchen","hook-codex"]` rides the command line, independent of `CODEX_HOME`. Confirmed firing under the fresh home.

## STEP-1 de-risk results (all held)

1. **notify under fresh seeded `CODEX_HOME`** → ✅ a turn fired `kitchen hook-codex`, writing the cook's status JSON to the sous status dir.
2. **trust prompt** → `--dangerously-bypass-approvals-and-sandbox` does NOT suppress codex's workspace-trust prompt under a fresh home, and a `-c projects."<cwd>".trust_level` override did NOT gate it either. The working fix (and what `_seed_codex_home` writes) is a persisted `config.toml`:
   ```toml
   [projects."/Users/plucas/cncorp/claude-kitchen"]
   trust_level = "trusted"
   ```
   With it pre-seeded, codex booted straight to the prompt — no trust dialog.
3. **auth sufficiency** → ✅ `auth.json` alone authenticates; no dependence on `version.json`/`installation_id`/token-cache.

## End-to-end (real code paths: `_seed_codex_home` + `build_shell_cmd`)

Cook launched with the exact command `kitchen hire --clean-room --backend codex` produces, driven through one turn with an injection probe. Result:

```
# injection probe answer in the cook's TUI:
SP=NO   MEM=NO   AGENTS=NO

# cook->sous notify status file written by `kitchen hook-codex`:
{"agent": "e2ecook", "session": "ck-e2e", "status": "idle",
 "summary": "SP=NO\nMEM=NO\nAGENTS=NO", "session_id": "019f0560-..."}
```

No `MEMORY.md` was present in the fresh home. `--backend gemini --clean-room` still fails loud.

## Cleanup

Per-cook `CODEX_HOME` dirs live under `<kitchen-base>/codex-home/<name>` and are removed on `kitchen clock-out <cook>` (per-cook) and `kitchen close` (whole `codex-home/`).

---

# `--with-skill` — opt a custom skill into a clean-room cook (Claude, allowlist v1)

`kitchen hire <name> --clean-room --with-skill <path>` (repeatable, Claude only) loads a
custom skill/plugin dir into the blank cook via a session-scoped `--plugin-dir` per path —
additive only; memory/superpowers/role stay off and the sous-managed cwd is untouched.
Captured **2026-06-26**, Claude Code **2.1.190**, subscription auth.

## Mechanism

`--plugin-dir <path>` loads **both** a bare skill dir (containing `SKILL.md`) and a
plugin-wrapped skill (containing `.claude-plugin/plugin.json`) — both verified to surface the
skill while clean-room guarantees hold. Chosen over staging into `<cwd>/.claude/skills` because
it's session-scoped and doesn't mutate cwd.

## Exact command `build_shell_cmd(..., clean_room=True, plugin_dirs=[<skill>])` emits

```
bash -lc 'export AGENT_NAME=eval1 AGENT_SESSION=ck-eval STATUS_DIR=… KITCHEN_NOTES=… KITCHEN_WIKI=…; CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 exec claude --dangerously-skip-permissions --disallowedTools AskUserQuestion --settings '{"enabledPlugins":{"superpowers@superpowers-marketplace":false}}' --plugin-dir /…/myskill'
```

Memory env-var before `exec`; superpowers-off `--settings`; the opt-in `--plugin-dir`; no role.

## End-to-end (real `build_shell_cmd` flags)

Skill dir: `myskill/SKILL.md` with `description: Hand-written test skill for clean-room with-skill verification.` Probe of the launched cook:

```
SP=NO ; MEM=NO ; SKILL=Hand-written test skill for clean-room with-skill verification.
```

Clean-room guarantees intact (superpowers injection off, memory off) **and** the named skill
available — its `SKILL.md` description read back verbatim (not hallucinated). Negative control
(same flags, no `--plugin-dir`) → `SKILL=NONE`, confirming the skill's presence is due to the opt-in.

## Guards

`--with-skill` without `--clean-room` fails loud; on codex/gemini fails loud ("not yet supported");
a path that isn't a dir, or a dir lacking both `SKILL.md` and `.claude-plugin/plugin.json`, fails clearly.
