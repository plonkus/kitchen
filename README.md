# claude-kitchen

Multi-agent orchestration for Claude Code and Codex. A **sous chef** (Claude) coordinates **cook** agents running in tmux windows, communicating via Claude Code's channels feature.

**→ [How it works](ARCHITECTURE.md)** — the architecture in two diagrams: tmux keystrokes down, hooks + MCP channels up, any agent CLI as a cook, all on your existing subscriptions.

## Requirements

Every dependency below lists **why** kitchen needs it, a **verify** command to check whether it's already present, and how to **install** it if missing. An agent can walk this list top to bottom, run each verify command, and install only what's missing. The [Install](#install) section then ties it together, ending with `kitchen setup` as the final green-light check.

### Required

- **tmux** — the entire orchestration runs in tmux windows; nothing works without it.
  - Verify: `tmux -V`
  - Install: `brew install tmux` (macOS) · `apt install tmux` / `dnf install tmux` (Linux)
- **git** — kitchen derives a per-project state slug from `git remote origin` (falls back to the repo's absolute toplevel path for local-only repos), and `kitchen open --src` uses git worktrees. Almost always already installed.
  - Verify: `git --version`
  - Install: `brew install git` (macOS) · distro package manager (Linux)
- **[uv](https://docs.astral.sh/uv/)** — kitchen installs as a uv tool, and uv provides the Python 3.12+ runtime the CLI needs.
  - Verify: `uv --version`
  - Install: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Claude Code CLI ≥ 2.1.80** — kitchen uses the development channels feature added in 2.1.80; older versions can't run a kitchen.
  - Verify: `claude --version` (must be ≥ 2.1.80)
  - Install: see [claude.com/claude-code](https://claude.com/claude-code)
- **claude.ai web auth (not an API key)** — channels are only exposed to claude.ai logins. Console / API-key auth fails at channel-connect time.
  - Verify: run `claude` and check `/login` shows a logged-in claude.ai account
  - Fix: `claude /login` and pick your claude.ai account
- **superpowers plugin** — kitchen's `sous-chef.md` workflows reference its skills.
  - Verify: `claude /plugin list` shows `superpowers`, or `~/.claude/plugins/cache/superpowers-marketplace/superpowers/` exists
  - Install (from inside Claude Code): `/plugin install superpowers from superpowers-marketplace`
- **`mcp` Python SDK** — `channel.py` is an MCP server. **No manual install needed** — it's a transitive dependency pulled in automatically by `uv tool install` below. Listed here only so it isn't mistaken for a separate step.
  - Verify (after install): `uv run python -c "import mcp"`

### Optional

- **Codex CLI** — only needed for Codex backend cooks (`kitchen hire <name> --backend codex`).
  - Verify: `codex --version`
  - Install: see OpenAI's Codex CLI install docs
- **Codex hooks config** — if Codex is installed, `~/.codex/config.toml` must enable hooks under `[features]`: `hooks = true` (or the older `codex_hooks = true`). `kitchen setup` checks this and nudges if missing.

## Install

Follow these steps in order. Each maps to a dependency in [Requirements](#requirements) above — verify first, install only what's missing.

**1. Install the required system tools** (tmux, git, uv):

```bash
brew install tmux git                              # macOS (git often already present)
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv
```

**2. Install the Claude Code CLI and log in via web auth:**

Install Claude Code per [claude.com/claude-code](https://claude.com/claude-code), then:

```bash
claude /login   # pick your claude.ai account — NOT an API key (channels need web auth)
```

**3. Install the superpowers plugin** from inside Claude Code:

```
/plugin install superpowers from superpowers-marketplace
```

**4. Clone the kitchen repo and install the CLI:**

```bash
git clone git@github.com:plonkus/kitchen.git
cd kitchen
uv tool install --editable .   # also pulls in the `mcp` SDK transitively
```

`--editable` means edits to this checkout are picked up immediately by the installed `kitchen` binary — convenient for contributors, harmless for everyone else.

**5. Run the diagnostic** — this is the verification gate:

```bash
kitchen setup
```

`kitchen setup` checks your environment and auto-installs the skill symlink. It does **not** auto-install hooks — it prints the exact JSON / TOML to paste into your settings. Specifically, it verifies:

1. Claude Code hooks (`Stop` + `UserPromptSubmit`) in `~/.claude/settings.json`
2. Codex hook (`notify = ["kitchen", "hook-codex"]`) in `~/.codex/config.toml`
3. The `claude-kitchen` skill is symlinked into `~/.claude/skills/` (auto-installed)
4. The `mcp` Python SDK is importable
5. The superpowers plugin exists at `~/.claude/plugins/cache/superpowers-marketplace/superpowers`
6. The `claude` CLI is on PATH and is version ≥ 2.1.80
7. No legacy kitchen named `projects` (the name is reserved for the per-project wiki root)

If any check fails, `kitchen setup` exits non-zero and tells you what to fix. Re-run it until it reports everything green.

## Quick Start

`cd` into any git repo, then:

```bash
# Open a kitchen in the current repo (no worktree)
kitchen open

# Or: open a named kitchen with a git worktree at a sibling path
kitchen open my-feature
```

The sous chef launches Claude in your current terminal. Talk to it in natural language — it handles the orchestration. In a separate terminal, watch cooks work:

```bash
tmux attach -t ck-<kitchen-name>
```

(When you omit `<name>`, the kitchen name is the repo directory's name.)

The kitchen is automatically namespaced by the project's slug — the repo name,
taken from the git remote (`git@github.com:owner/my-project.git` → `my-project`).
`kitchen open main` becomes `my-project-main`, so the same name in two different
repos never collides on the tmux session, state dir, or channel socket. The
`tmux attach -t` target above is `ck-<project-slug>-<name>`. (Two unrelated repos
that share a name collide; disambiguate with an explicit kitchen name.)

A kitchen opened before namespacing existed lives at the bare `<name>`. The first
`kitchen open <name>` from its project root re-attaches it (with a one-line
suggestion to close+reopen under the namespaced name) instead of forking a new
kitchen; bare-name lookups (`kitchen close <name>`, etc.) keep resolving from
inside the project root.

## Usage

Once the kitchen is open, just talk to the sous chef. It knows how to hire cooks, send them work, and manage the workflow:

```
> hire a claude cook to fix the auth bug in src/auth.py

> spin up a codex worker to review the last PR

> fire up 3 cooks — one for tests, one for docs, one for the migration

> have the reviewer look at what eng just did

> clock out the docs cook, we're done with docs
```

The sous chef translates your requests into `kitchen hire`, `kitchen ticket`, etc. You don't need to remember the CLI — the sous handles it. You can also run the CLI directly from another terminal if you prefer.

## How It Works

1. `kitchen open` launches Claude as your sous chef, with a per-kitchen MCP channel server
2. The sous hires cooks that run in tmux windows (you can watch them live)
3. When a cook finishes, its Claude Code / Codex hook sends a notification through the channel socket
4. The MCP server surfaces that notification to the sous as `← kitchen: <cook's response>`; the sous decides what's next

The sous is autonomous — give it a goal and it hires cooks, assigns work, reviews results, and iterates.

For the full picture — diagrams, the hook → socket → channel pipeline, the verified tmux typing layer, and how to add new cook backends — see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## CLI Reference

These are the commands the sous chef uses under the hood. You can also run them directly.

| Command | What it does |
|---------|-------------|
| `kitchen open [name] [project]` | Start a kitchen with sous chef |
| `kitchen hire <cook> --backend claude\|codex [--role <role>]` | Spawn a cook (optionally with a role; works for both backends) |
| `kitchen roles` | List available cook roles |
| `kitchen ticket <cook> "message"` | Send a task to a cook |
| `kitchen peek <cook> [--full]` | See a cook's screen |
| `kitchen brigade` | Status of all cooks |
| `kitchen clock-out <cook>` | Kill a cook |
| `kitchen close` | Shut down the kitchen |
| `kitchen setup` | Check hooks, skill, and dependencies |

### Options

- `kitchen hire <cook> --role <role>` — inject a role prompt at boot (Claude via `--append-system-prompt-file`, Codex via a first message). See `kitchen roles` for available roles.
- `kitchen hire <cook> --effort max` — set reasoning effort (`low` / `medium` / `high` / `max`)
- `kitchen hire <cook> --backend codex` — use Codex instead of Claude

## Where State Lives

All kitchen state lives under `~/.claude-kitchen/`:

- `~/.claude-kitchen/<kitchen-name>/` — per-kitchen state: `kitchen.json`, `kitchen.sock` (MCP socket), `sous.pid`, `cooks/` (cook status JSON), `notes/` (handoff, log, task briefs — wiped on `kitchen close`)
- `~/.claude-kitchen/projects/<project-slug>/wiki/` — the per-project **wiki**, persistent across kitchens. Contains `mistakes.md` (lessons learned) and `preferences.md` (head chef's working style). Survives `kitchen close`.

`<project-slug>` comes from `git remote origin` — e.g. `my-project` for a repo at `git@github.com:owner/my-project.git`. Local-only repos (no origin) fall back to a slugified toplevel path.

## Customizing kitchen

Kitchen ships opinionated workflow prompts that you can tune to your own style:

- **`sous-chef.md`** — the sous chef's orchestration prompt (injected via `--append-system-prompt` on `kitchen open`).
- **`roles/`** — per-role cook prompts (`eng.md`, `qa.md`, `reviewer.md`, `_default.md`).

Both live inside the installed `claude_kitchen` package — `kitchen setup` prints their exact path on success. Because the CLI is installed `--editable`, edits to these files in your checkout are live immediately; just re-open the kitchen (and re-hire cooks) to pick them up. For larger or persistent changes, fork the repo and install your fork.

## Troubleshooting

- **`server:kitchen · no MCP server configured` at sous startup.** Harmless race — the channel server connects a moment later. Ignore.
- **`kitchen setup` says hooks are missing.** It prints the exact snippets to paste into `~/.claude/settings.json` (Claude hooks) and `~/.codex/config.toml` (Codex hook). Paste them, then re-run `kitchen setup`.
- **`kitchen setup` says the superpowers plugin is missing.** Install from inside Claude Code: `/plugin install superpowers from superpowers-marketplace`.
- **Channel errors on `kitchen open` ("not authenticated" / channel refused).** Channels require claude.ai auth. Run `claude /login` and pick your claude.ai account — Console / API-key auth does not expose channels.
- **`kitchen setup` reports Claude CLI too old.** Upgrade Claude Code to ≥ 2.1.80.
- **Legacy kitchen named `projects`.** The `projects` name is reserved for the per-project wiki root. Rename the legacy state directory as `kitchen setup` instructs.
- **Kitchen refuses to open ("not a git repository").** `cd` into a git repo first; kitchen needs one to derive a state slug.

## Acknowledgements

Kitchen was inspired by two projects we admired and learned from. We re-implemented their ideas in our own Python — no code was copied.

- **[firstmate](https://github.com/kunchenguid/firstmate)** — we studied its tmux send/verify approach (verified-submit, prompt-suggestion/ghost-text suppression, busy-footer detection) and re-implemented those ideas ourselves.
- **[mypeople](https://github.com/plow-pbc/mypeople)** — general inspiration for orchestrating multiple Claude Code agents through a single channel server.
