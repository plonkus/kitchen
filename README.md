# Kitchen

**Keep one AI focused on the big picture while a brigade of AI coding agents does the work.**

Kitchen turns a Claude Code session into a hands-on engineering lead. That **sous chef** hires Claude, Codex, or Gemini **cooks** in tmux, gives them focused tickets, and receives their completed work automatically through Claude Code channels. You keep talking to one coherent orchestrator while the cooks spend their own context reading code, running tests, implementing changes, and reviewing one another.

It runs the stock agent CLIs you already use—authenticated through your existing subscriptions—not stripped-down API workers hidden behind a custom harness.

## Why kitchen?

| Benefit | Why it matters |
|---|---|
| **A manager that stays sharp** | The sous delegates source reads, greps, tests, and implementation instead of filling its own context with project detail. Its context stays available for coordination, tradeoffs, and long-running continuity. |
| **Real agents, on your existing subscriptions** | Cooks are ordinary Claude Code, Codex, and optionally Gemini/agy sessions. They keep their native tools, skills, plugins, MCP servers, and subscription auth; no separate model API integration or metered API-key billing is required. Provider usage limits still apply. |
| **An opinionated workflow you can actually edit** | `sous-chef.md` and the role prompts encode a complete operating method: delegate investigation, write verifiable tickets, review across backends, preserve decisions, and prove completion. They are plain Markdown—fork them, tune them, or replace the methodology. |
| **Cross-model review by design** | The supplied workflow pairs a Claude implementer with a Codex reviewer, or vice versa. An adversarial second model often catches assumptions the first model rationalized past. The convention lives in the prompt, so you remain in control. |
| **Parallel work without a black box** | Each cook is an isolated tmux window. Run independent workstreams concurrently, attach to watch them live, inspect a pane with `kitchen peek`, or take over the keyboard yourself. |
| **Memory that survives the shift** | Per-project `mistakes.md` and `preferences.md` files persist across kitchens, while per-kitchen notes support handoffs and task briefs. The team can carry forward lessons without keeping every old conversation alive. |

Kitchen also integrates the superpowers skills workflow for brainstorming, specs, chunked implementation, review, and verification. For heavier use cases it can launch a child kitchen for an independent workstream or a clean-room Claude/Codex cook for a more reproducible evaluation—but the core stays deliberately small: tmux down, hooks and a Unix socket back up.

## The mental model

```text
                         your terminal
  you  <────────────>  sous chef (Claude Code)
                              │       ▲
                kitchen ticket│       │MCP channel notification
                              ▼       │
                    tmux session      │
                 ┌────────┼────────┐  │
                 │        │        │  │
              Claude    Codex   Gemini/agy
               cook      cook      cook
                 │        │        │
                 └────────┴────────┘
                      completion hooks
                              │
                    per-kitchen Unix socket
```

There are two intentionally simple directions:

- **Sous → cook:** `kitchen ticket` pastes a message into the cook's tmux composer and verifies that the TUI accepted it.
- **Cook → sous:** the agent CLI's completion hook sends the cook's full final response to a per-kitchen Unix socket. A tiny MCP channel server pushes it into the sous's live context—no polling and no pane scraping for results.

For the implementation details, diagrams, backend contracts, and the hook → socket → MCP flow, see [How kitchen works](ARCHITECTURE.md).

## A quick taste

Open a kitchen in a Git repository:

```bash
kitchen open
```

Claude starts as the sous chef in your terminal. Give it the outcome you want:

```text
Build the import feature. Have one model implement it, another review it,
and run an end-to-end verification before you report back.
```

The sous can turn that into a flow like this:

```bash
kitchen hire eng --backend claude --role eng
kitchen hire reviewer --backend codex --role reviewer
kitchen ticket eng "Implement the agreed import feature and verify it end to end."

# eng's response arrives automatically in the sous's context
kitchen ticket reviewer "Review the import implementation. Do not edit; report findings."
```

You continue talking to the sous while both cook sessions remain visible in tmux. The supplied workflow reuses cooks when their context is valuable, routes findings back to the implementer, and requires evidence before declaring the work done.

## Requirements

### Required

- **tmux** — cooks run in tmux windows; this is the process and observation layer.
  - Verify: `tmux -V`
  - Install: `brew install tmux` on macOS, or use your Linux package manager.
- **git** — kitchen derives the project namespace from the repository and uses git worktrees for named kitchens.
  - Verify: `git --version`
- **[uv](https://docs.astral.sh/uv/)** — installs the Python 3.12+ CLI and its dependencies.
  - Verify: `uv --version`
- **Claude Code CLI 2.1.80 or newer** — the sous uses Claude Code's development channels feature.
  - Verify: `claude --version`
- **claude.ai web authentication** — channels are available to claude.ai logins, not Console/API-key authentication.
  - Sign in: `claude /login`, then choose your claude.ai account.
- **superpowers plugin** — the supplied sous workflow invokes its brainstorming and development skills.
  - Install from Claude Code: `/plugin install superpowers from superpowers-marketplace`
- **`mcp` Python SDK** — installed automatically as a transitive dependency of this project; no separate install step is needed.

### Optional backends and tools

- **Codex CLI** — required only for `--backend codex` cooks. Kitchen forces its completion notify command per cook launch.
- **Codex hook support** — `~/.codex/config.toml` should contain `notify = ["kitchen", "hook-codex"]` and `hooks = true` under `[features]`. `kitchen setup` currently checks for the notify stanza even if you do not plan to hire Codex cooks.
- **Antigravity CLI (`agy`)** — required only for the opt-in `--backend gemini` cook. Kitchen does not use a `gemini` binary and `kitchen setup` does not install or check `agy`.
- **`jq`** — needed only by the packaged richer statusline example, not by the kitchen core.

## Install

### 1. Install the system tools

On macOS:

```bash
brew install tmux git
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Linux, install tmux and git through your package manager, then install uv with the command above.

### 2. Install Claude Code and authenticate

Install Claude Code using its official instructions, then sign in with a claude.ai account:

```bash
claude /login
```

Kitchen's channel connection will not work with Console/API-key authentication.

### 3. Install superpowers

From inside Claude Code:

```text
/plugin install superpowers from superpowers-marketplace
```

### 4. Clone and install kitchen

```bash
git clone git@github.com:plonkus/kitchen.git
cd kitchen
uv tool install --editable .
```

The editable install makes changes to this checkout—including prompt customizations—available to the installed `kitchen` command immediately.

### 5. Run the setup diagnostic

```bash
kitchen setup
```

`kitchen setup` verifies:

1. Claude `Stop` and `UserPromptSubmit` hook configuration.
2. The Codex completion notify configuration.
3. The `claude-kitchen` skill symlink, creating or updating it automatically.
4. The `mcp` Python SDK.
5. The superpowers plugin.
6. Claude Code version 2.1.80 or newer.
7. That no legacy kitchen uses the reserved name `projects`.

The statusline check is advisory. Hook installation is **not** automatic: when hook configuration is missing, setup prints the exact JSON or TOML to add. Re-run the command until the blocking checks are green.

## Quick start

Run kitchen from inside any git repository:

```bash
# Use the current checkout
kitchen open

# Or create a branch + sibling worktree for a named kitchen
kitchen open my-feature
```

The current process becomes the Claude sous chef. Talk to it normally—describe goals, constraints, and what evidence you expect. The sous knows the kitchen commands and manages cooks for you.

In another terminal, watch the brigade:

```bash
tmux -L ck-<project-slug>-<kitchen-name> attach -t ck-<project-slug>-<kitchen-name>
```

Each kitchen runs on its own tmux server (socket `ck-<kitchen>`), so the `-L` is required — a bare `tmux attach -t ck-…` will not find the session. `kitchen open` prints the exact command.

Kitchen namespaces sessions and state by project slug. For example, `kitchen open my-feature` in a repository named `widget` becomes kitchen `widget-my-feature`, while the git branch and worktree keep the name `my-feature`. A no-name `kitchen open` usually uses the repository name without doubling it.

### Useful requests to give the sous

```text
Hire a Claude engineer to investigate the flaky integration test.

Have a Codex reviewer inspect the engineer's commit for correctness and scope.

Split the migration into independent workstreams and run them in parallel.

Use a clean-room cook in /tmp/eval-checkout to evaluate this prompt.

Clock out the docs cook; keep the reviewer around for the next change.
```

## CLI reference

The sous runs these commands under the hood. You can also use them directly from another terminal.

| Command | What it does |
|---|---|
| `kitchen open [name] [project]` | Open a kitchen; an explicit name creates a worktree. |
| `kitchen open <name> --sub-sous` | Launch a fresh child kitchen whose sous runs in its own tmux session. |
| `kitchen hire <cook> [options]` | Spawn a cook and wait until its interactive prompt is ready. |
| `kitchen roles` | List the packaged cook roles. |
| `kitchen ticket <cook> "message"` | Deliver a verified message to a cook. |
| `kitchen peek <cook> [--full]` | Capture the cook's current pane or full scrollback. |
| `kitchen brigade [kitchen]` | Show live cook status and known context usage. |
| `kitchen clock-out <cook>` | Hard-kill a cook and remove its managed state. |
| `kitchen sweep` | Remove state files for cook windows that no longer exist. |
| `kitchen close [kitchen]` | Shut down the kitchen and remove managed per-kitchen state. |
| `kitchen setup` | Check hooks, dependencies, skill installation, and environment readiness. |

### Hire options

```bash
# Packaged role; _default is used when --role is omitted
kitchen hire eng --backend claude --role eng

# Cross-model reviewer
kitchen hire reviewer --backend codex --role reviewer

# Unified effort scale: low, medium, high, max
kitchen hire investigator --effort max

# Claude model tier; the Claude CLI resolves the current model in that tier
kitchen hire architect --model opus

# Near-fresh eval worker: no role, auto-memory, or superpowers startup injection
kitchen hire eval1 --clean-room --project /absolute/eval-directory

# Add a specific skill/plugin directory back to a clean-room Claude cook
kitchen hire eval2 --clean-room --with-skill /absolute/path/to/skill
```

Claude cooks receive role Markdown as an appended system prompt. Codex receives it as the first message after the TUI is ready, and Gemini/agy receives it as its initial turn. Clean-room mode supports Claude and Codex; `--with-skill` currently supports Claude only. Clean-room mode does not hide files such as `CLAUDE.md` or `AGENTS.md` from the cook's working directory, so use an empty directory or pinned checkout when that isolation matters.

## How completion notifications work

Every launched agent gets `AGENT_NAME`, `AGENT_SESSION`, and `STATUS_DIR`. Global hooks are gated on those variables, so ordinary agent sessions outside a kitchen are untouched.

When a cook completes a turn:

1. Claude's `Stop` hook, Codex's `notify`, or agy's stop hook invokes kitchen.
2. Kitchen stores the full final response and any available context information in the cook's status JSON.
3. The hook sends one JSON line to `~/.claude-kitchen/<kitchen>/kitchen.sock`.
4. The per-kitchen MCP server emits a Claude channel notification.
5. The response appears automatically in the sous's context, and the sous decides whether to follow up, review, iterate, or report back.

The root sous's own Stop hook does not notify its own socket, preventing an echo loop. Child sous sessions are the deliberate exception: they can report completion to their parent kitchen.

## Where state lives

```text
~/.claude-kitchen/
├── <kitchen>/
│   ├── kitchen.json          # source/worktree + sous resume session
│   ├── kitchen-mcp.json      # generated per-kitchen MCP config
│   ├── kitchen.sock          # live channel socket
│   ├── sous.pid              # duplicate-sous guard
│   ├── cooks/<name>.json     # backend, status, response, session, context
│   ├── codex-home/           # clean-room Codex homes, when used
│   └── notes/                # handoff, log, and long task briefs
└── projects/<slug>/wiki/
    ├── mistakes.md           # durable lessons
    └── preferences.md        # durable working preferences
```

`kitchen close` removes managed per-kitchen notes and cook state. The project wiki survives so later kitchens inherit the lessons and preferences.

## Customize the workflow

The orchestration behavior is intentionally not hard-coded:

- **`src/claude_kitchen/sous-chef.md`** defines the manager's operating method: protect context, delegate investigation, use superpowers for spec work, review across models, preserve decisions, and require evidence.
- **`src/claude_kitchen/roles/`** contains the cook contracts: `eng`, `reviewer`, `qa`, and `_default`.
- **`.kitchen/on-open.sh` and `.kitchen/on-close.sh`** in a project can run lifecycle automation for that repository.

With an editable install, reopen the kitchen or re-hire a cook after changing a prompt to pick up the new behavior. For long-lived customization, fork the repository and install your fork.

## Troubleshooting

- **`server:kitchen · no MCP server configured` at sous startup:** this is a known harmless startup race; the server connects shortly afterward.
- **Setup says hooks are missing:** copy the exact Claude JSON or Codex TOML that `kitchen setup` prints, then rerun it.
- **Superpowers is missing:** install it from Claude Code with `/plugin install superpowers from superpowers-marketplace`.
- **Channel authentication fails:** run `claude /login` and select a claude.ai account. Console/API-key authentication does not expose channels.
- **Claude Code is too old:** upgrade to version 2.1.80 or newer.
- **`agy not on PATH`:** Gemini cooks require Antigravity CLI; Claude and Codex kitchens do not.
- **Kitchen refuses to open outside a git repository:** move into a git repository first. Kitchen needs it for project namespacing and optional worktrees.
- **Legacy kitchen named `projects`:** that name is reserved for the persistent project wiki root; follow the rename instruction printed by setup.

## Acknowledgements

Kitchen was inspired by two projects we admired and learned from. We re-implemented their ideas in our own Python—no code was copied.

- **[firstmate](https://github.com/kunchenguid/firstmate)** — we studied its tmux send/verify approach (verified-submit, prompt-suggestion/ghost-text suppression, busy-footer detection) and re-implemented those ideas ourselves.
- **[mypeople](https://github.com/plow-pbc/mypeople)** — general inspiration for orchestrating multiple Claude Code agents through a single channel server.
