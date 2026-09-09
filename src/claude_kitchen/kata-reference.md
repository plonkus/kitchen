# kata verb reference

Written against **kata v0.17.2** (darwin/arm64, Homebrew Core). Source of truth is the
binary: `kata quickstart`, `kata <cmd> --help`, `kata openapi`, and the docs at
<https://www.katatracker.com/docs/>. Where the website and the binary disagree, the
binary wins — see [Surprises](#surprises).

## 1. Mental model

kata is a local-first issue tracker. A **daemon** on your machine owns a SQLite database
(`~/.kata/kata.db`) and answers over a Unix socket; every CLI command, the TUI, the web UI
and the MCP server are clients of it. Issues live in a **project**; a **workspace** (a
directory tree) resolves to a project by walking up to the nearest committed `.kata.toml`,
falling back to the repo's git remote — which is why a git worktree of a bound repo
resolves the same project even without the file. Each issue has a 26-char **uid** (a ULID,
stable forever, survives `move` between projects) and a 4-char **short_id** derived from it
(`abc4`, or `kata#abc4` across projects) — the short_id is what you type. An issue is
**open** or **closed**; orthogonal to that is **work.attention**, a metadata key
(`ok` / `needs-human` / `stuck`) that `kata wait` blocks on, so an agent can flag "I'm stuck"
without closing anything. Issues carry **relationships** framed from the issue's own point
of view — `parent` (containment; does not gate readiness), `blocks` (this must finish first),
`blocked_by` (the other must finish first), `related` (symmetric, no ordering) — plus free-form
**metadata** (JSON values; `scheduled_on` and `deadline_on` are just reserved keys with
`schedule`/`deadline` as sugar), **labels**, and typed **close evidence**: closing asserts
completion, so it wants a reason (`done|wontfix|duplicate|superseded|audit-no-change`), a
substantive `--message`, and machine-checkable `--evidence` (`commit:`, `pr:`, `test:`,
`reviewed-paths:`, `external:`, `no-change-audit:`, `duplicate-of:`, `superseded-by:`).
Everything that happens is appended to a monotonic **event** log (`issue.created`,
`issue.labeled`, `issue.closed`, …) that `events`, `digest` and `audit` read back, and that
federation replicates.

Conventions used below: `<ref>` is a short_id, qualified short_id, or full ULID.
Global flags on nearly every command: `--agent` (concise agent-readable text — the default
you want in logs), `--json` (full structured output for `jq`), `--format human|json|agent`,
`--project NAME`, `--workspace PATH`, `--daemon NAME`, `--as ACTOR`, `-q`.

## 2. The verbs

### Create, edit, close

**`create <title>`** — new issue. `--body/--body-file/--body-stdin`, `--label`, `--meta k=v`,
`--owner`, `--priority 0..4` (0 highest), `--parent/--blocks/--blocked-by/--related`,
`--idempotency-key` (safe retry), `--force-new` (bypass the look-alike soft-block).
```
kata create "fix login race" --body "double submit in Safari" --idempotency-key login-race-2026-05-02 --agent
```

**`edit <ref>`** — change title/body/owner/priority and links. `--title`, `--body`,
`--owner`, `--priority N|-`, the four link flags plus `--remove-parent/--remove-blocks/`
`--remove-blocked-by/--remove-related`, `--comment TEXT`.
```
kata edit abc4 --priority 1 --blocked-by d4ex --comment "waiting on the migration" --agent
```

**`close <ref>`** — assert the work is done. `--reason` or the sugar `--done/--wontfix/`
`--duplicate-of/--superseded-by/--audit-no-change`; `--message` (substantive prose);
`--evidence type:value` (repeatable) or sugar `--commit/--pr/--test/--reviewed`;
`--dry-run`, `--if-match rev-N`, `--idempotency-key`, `--comment`.
```
kata close abc4 --done --message "Fixed the Safari double-submit; regression test added." --test "pytest -k login"
```

**`reopen <ref>`** — undo a close. `--comment`.
```
kata reopen abc4 --comment "regressed on 0.18" --agent
```

**`comment <ref>`** — append a comment. `-m/--body`, `--body-file`, `--body-stdin`.
```
kata comment abc4 --body "Found a second reproduction path." --agent
```

**`comment edit <ref> <comment-uid>`** — rewrite a comment body (same body flags).
```
kata comment edit abc4 01M23VCBGDAEET8SJGZ3XR6GSX --body "corrected: it is the callback, not the form"
```

**`show <ref>`** — issue plus labels, metadata, body, comments, events. `--render` renders
Markdown when stdout is a terminal.
```
kata show abc4 --agent
```

**`status <ref>`** — one line of identity and hold state (project, revision, actor, auth,
instance uid, `hold=`).
```
kata status abc4 --agent
```

**`move <ref> <project>`** — move an issue to another project; keeps uid, comments, history
and links (which may then span projects), assigns a fresh short_id. `--dry-run`, `--comment`.
```
kata move abc4 other-project --dry-run
```

**`delete <ref>`** — soft-delete. Requires **both** `--force` and
`--confirm "DELETE <project>#<short_id>"`. Reversible with `restore`.
```
kata delete abc4 --force --confirm "DELETE claude-kitchen#abc4"
```

**`restore <ref>`** — undo a soft delete.
```
kata restore abc4 --agent
```

**`purge <ref>`** — irreversible removal of the issue and all its rows. `--force`,
`--confirm "PURGE <short_id>"`, `--reason`.
```
kata purge abc4 --force --confirm "PURGE abc4" --reason "contained a secret"
```

### Relationships

Relationships are set through `create`/`edit` flags, not their own verbs, and read through
`show` or the web UI's reachable-graph view.

- `--parent <ref>` — this issue is a sub-task of `<ref>`. At most one. Containment only: a
  parent does **not** gate the child's readiness. The daemon refuses to close a parent while
  open children remain.
- `--blocks <ref>` — this must finish before `<ref>` can proceed.
- `--blocked-by <ref>` — `<ref>` must finish before this can. Drives `ready`/`next`.
- `--related <ref>` — symmetric, no ordering.
- `--remove-parent` is **strict** (must equal the current parent or 409); the other three
  `--remove-*` flags are idempotent.

```
kata create "fix auth flow" --parent abc4 --blocked-by d4ex --related j7m2 --agent
kata edit abc4 --remove-blocks d4ex --agent
```

### Ownership and leases

**`assign <ref> <owner>`** — set the owner outright. `--comment`.
```
kata assign abc4 patrick --agent
```

**`claim <ref>`** — take ownership as the current actor. `--if-unowned` (only if free),
`--force` (steal), `--comment`. The safe primitive for multi-agent queues.
```
kata claim abc4 --if-unowned --agent
```

**`unassign <ref>`** — clear the owner. `--expect-owner NAME` guards against races.
```
kata unassign abc4 --expect-owner eng --agent
```

**`whoami`** — resolved actor and where it came from (`$KATA_AUTHOR` > `$USER` > git).
```
kata whoami --agent
```

Federation write **leases** are a separate, stronger lock than ownership; see
[Federation](#federation-sync-bridge-connectors).

### Metadata and labels

**`meta get <ref> [key]`** — read metadata. Values are JSON, so a string prints quoted.
```
kata meta get abc4 --agent
```

**`meta set <ref> <key> <value>`** — write one key. `--json-value` (value is raw JSON, not a
string), `--if-absent`, `--if-value OLD`, `--if-match rev-N` for compare-and-swap.
```
kata meta set abc4 work.attention needs-human --agent
```

**`meta unset <ref> <key>`** — clear a key. `--if-value`, `--if-match`, `--json-value`.
```
kata meta unset abc4 work.attention --agent
```

**`label add <ref> <label>`** / **`label rm <ref> <label>`** — attach/detach one label.
`--comment` on both.
```
kata label add abc4 needs-review --agent
```

**`labels`** — label counts for the current project.
```
kata labels --agent
```

### Scheduling

Both write reserved metadata keys and both accept `YYYY-MM-DD`, local
`YYYY-MM-DDTHH:MM[:SS]`, an RFC 3339 `…Z` instant, or `-` to clear. `--if-match` on both.

**`schedule <ref> <date|->`** — sets `scheduled_on`. A future value **parks** the issue: it
disappears from `ready` and `next` but stays in `list`.
```
kata schedule abc4 2026-10-01     # park
kata schedule abc4 -              # unpark
```

**`deadline <ref> <date|->`** — sets `deadline_on`. Informational: does **not** park.
```
kata deadline abc4 2026-12-01 --agent
```

For "someday, no date", the quickstart's idiom is a marker key you remove later:
`kata meta set abc4 someday true --json-value`.

### Querying

**`list`** — issues in the workspace's project. `--status open|closed|all` (default open),
`--label/--no-label`, `--meta k` or `k=v`, `--owner`, `--unowned`, `--priority`,
`--max-priority`, `--limit` (default 200), `--all` (every non-archived project).
```
kata list --status all --label bug --agent
```

**`ready`** — open issues with no open `blocks` predecessor and not parked by `scheduled_on`.
Same filter flags plus `--all`.
```
kata ready --unowned --no-label blocked --agent
```

**`next`** — the single highest-priority ready issue. `--unowned`, `--label/--no-label`,
`--owner`, `--full`, `--all`. The "what should I work on" verb.
```
kata next --unowned --agent
```

**`search <query>...`** — title/body/comment search. `--lexical` (default), `--hybrid`,
`--semantic`, `--label/--no-label`, `--include-deleted`, `--limit` (default 20).
```
kata search "login race" --agent
```

### Events, waiting, audit, digest

**`events`** — one-shot listing (`--after CURSOR`, `--limit`, default 100, id ASC) or a live
SSE stream with `--tail` (`--last-event-id N` to resume). `--all-projects`, `--project-id`.
```
kata events --after 0 --limit 100 --agent      # poll, remember the cursor
kata events --tail --last-event-id 53 --agent  # stream from a known point
```

**`wait <ref> [<ref>...]`** — block until issues finish or ask for help. `--until
closed|attention|needs-human|stuck` (default `closed`), `--all` (default) vs `--any`,
`--timeout 10m`, `--poll-interval`. Exit 8 on timeout. The fan-out/join primitive for an
orchestrator waiting on cooks.
```
kata wait abc4 d4ex --any --until attention --timeout 30m --json
```

**`audit closes`** — one row per `issue.closed` event, for catching agents that close in
bulk or without evidence. `--actor`, `--reason`, `--parent`, `--no-evidence`,
`--since/--until` (RFC 3339).
```
kata audit closes --no-evidence --agent
```

**`digest`** — human changelog over a window, grouped by actor. `--since 24h|RFC3339`,
`--until`, `--actor` (repeatable), `--project-id`, `--all-projects`.
```
kata digest --since 24h
```

### Project administration

**`init`** — bind the workspace: writes a committed `.kata.toml` and gitignores
`.kata.local.toml`. `--project NAME` (else derived from the git remote), `--replace`,
`--reassign`, `--with-agents` (write guidance into AGENTS.md/CLAUDE.md), `--with-hooks`
(Claude Code `.claude/` attention hooks), `--with-codex-hooks` (`.codex/hooks.json`).
```
kata init --project claude-kitchen
```

**`projects list`** — known projects with open/closed counts.
```
kata projects list --agent
```

**`projects show <project>`** — project detail plus its workspace **aliases** (the git
identities that resolve to it).
```
kata projects show claude-kitchen --json
```

**`projects create <name>`** — a daemon project with no workspace binding.
```
kata projects create scratchpad --agent
```

**`projects rename <project> <name>`** — rename in place.
```
kata projects rename old-name new-name
```

**`projects remove <project>`** — archive (hidden, events retained). `--force` when open
issues remain.
```
kata projects remove stale-project --force
```

**`projects restore <project>`** — un-archive.
```
kata projects restore stale-project
```

**`projects purge <project>`** — permanently delete an *archived* project and free the name.
`--force`, `--confirm "PURGE <project>"`, `--reason`.
```
kata projects purge stale-project --force --confirm "PURGE stale-project"
```

**`projects merge <source> <target>`** — fold one project into another.
`--rename-target NAME`.
```
kata projects merge old-repo new-repo --rename-target new-repo
```

**`projects detach <alias-identity>`** — drop one workspace alias. `--force` if it is the
project's only alias.
```
kata projects detach github.com/plonkus/kitchen
```

**`projects rewrite-author [project]`** — rewrite one author identity across a project
(pre-federation redaction; does not rewrite already-shared events). `--from`, `--to`.
```
kata projects rewrite-author claude-kitchen --from plucas --to patrick
```

**`export`** — dump the database as JSONL. `--output`, `--project-id`, `--include-deleted`
(default true), `--allow-running-daemon`.
```
kata export --output backup.jsonl --allow-running-daemon
```

**`import`** — load an export. `--input`, `--target` (SQLite path or Postgres DSN),
`--merge`, `--force`, `--new-instance`, `--source-format kata|beads`.
```
kata import --input backup.jsonl --target ./restored.db --new-instance
```

### Daemon, UI, TUI, MCP, tokens

**`daemon status`** — is it running, at what socket, which web-UI port, pid, uptime.
```
kata daemon status
```

**`daemon locate`** — print (and start, if local and stopped) the selected endpoint.
Resolution order: `--daemon` → `$KATA_SERVER` → `.kata.local.toml [server].url` →
`active_daemon` in config → local. Never prints credentials.
```
kata daemon locate --json
```

**`daemon start`** — start it. `--foreground`, `--listen host:port`, `--insecure-readonly`
(dev only).
```
kata daemon start --listen 127.0.0.1:7777
```

**`daemon stop`** / **`daemon restart`** / **`daemon reload`** — graceful shutdown; restart
(same `--listen`/`--insecure-readonly`); reload hook config without a restart.
```
kata daemon restart
```

**`daemon logs`** — daemon and hook-run logs. `--hooks`, `--failed-only`, `--event-type`,
`--hook-index`, `--limit`, `--tail`.
```
kata daemon logs --hooks --failed-only
```

**`health`** — db path, schema version, api schema version, uptime.
```
kata health --json
```

**`ui [<ref>]`** — open the web UI in the browser. Prints nothing at all; get the URL from
`kata daemon status`.
```
kata ui abc4
```

**`tui [<ref>]`** — Bubble Tea terminal UI scoped to the current project. `--mouse`,
`--uid-format none|short|full`. `?` for help, `q` to quit.
```
kata tui
```

**`mcp serve`** — expose kata as MCP tools. stdio by default; `--http host:port`,
`--http-token-env VAR`, `--projects`/`--all-projects`, `--storage-root`,
`--storage-target alias=path`, `--enable-token-admin`, `--trust-private-network`.
```
kata mcp serve --projects claude-kitchen
```

**`tokens create`** / **`tokens list`** / **`tokens revoke <id>`** — daemon identity tokens
for remote clients. `create` takes `--actor` and optional `--name`.
```
kata tokens create --actor ci-bot --name "github actions"
```

**`openapi`** — print the daemon's OpenAPI schema (generated in-process; no daemon needed).
`--format yaml|json`, `--version 3.1|3.0`.
```
kata openapi --format json > openapi.json
```

**`quickstart`** (alias `agent-instructions`) — the canonical agent briefing.
`--format contract` emits the short contract plus a state diagram.
```
kata quickstart --format contract
```

**`version`** — version, build time, go version, os/arch. Same as `--version`.

**`update`** — check for / install a release. `--check`, `-y/--yes`, `-f/--force`. Under
Homebrew, `--check` works but installing sends you back to `brew upgrade kata`.
```
kata update --check
```

**`help [command]`** — help for any command.

**`completion bash|zsh|fish|powershell`** — shell completion script.
```
kata completion zsh > "${fpath[1]}/_kata"
```

### Storage

**`storage postgres status`** — validate Postgres schema readiness without DDL.
`--dsn`, `--schema`.
```
kata storage postgres status --dsn "$KATA_DSN"
```

**`storage postgres migrate`** — install or advance the Postgres schema. Same flags.
```
kata storage postgres migrate --dsn "$KATA_DSN" --schema kata
```

### Federation, sync, bridge, connectors

Federation replicates a project between daemons (hub/spoke). Sync mirrors a project to an
external tracker. A bridge binds one issue to one external root, mediated by a connector.

**`federation identity`** — this daemon's instance uid (needed to enroll it as a spoke).
```
kata federation identity --json
```

**`federation enable [project]`** — turn a project into a hub.
```
kata federation enable claude-kitchen
```

**`federation enroll [project]`** — mint a spoke enrollment on the hub. `--spoke-instance`,
`--actor`, `--hub-url`, `--capabilities pull,push,lease`, `--token`, `--adopt-existing`,
`--allow-insecure`.
```
kata federation enroll claude-kitchen --spoke-instance 01M2... --actor laptop --hub-url https://hub:7777
```

**`federation join`** — join a hub as a spoke. `--hub-url`, `--token`, `--hub-project-id`
or `--hub-project-uid`, `--push`, `--capabilities`, `--baseline-through`,
`--replay-horizon`, `--adopt-existing`, `--allow-insecure`.
```
kata federation join --hub-url https://hub:7777 --token "$TOK" --hub-project-uid 01M2... --push
```

**`federation status`** — replication state for local bindings.
```
kata federation status --agent
```

**`federation leave [project]`** — revoke on the hub and tear down locally. `--delete`,
`--force`, `--local-only`, `--hub`, `--hub-token`, `--yes`, `--allow-insecure`.
```
kata federation leave claude-kitchen --yes
```

**`federation revoke <enrollment-id>`** — revoke one enrollment from the hub side.
```
kata federation revoke 3
```

**`federation enrollments list`** — audit hub enrollments.
```
kata federation enrollments list --json
```

**`federation rebind [project]`** — point spoke bindings at a configured HTTPS hub endpoint.
`--hub NAME`, `--all`.
```
kata federation rebind --all --hub prod-hub
```

**`federation lease acquire <ref>`** — take the federation write lease. `--ttl 30m` for a
timed lease (omit for an open-ended one).
```
kata federation lease acquire abc4 --ttl 2h
```

**`federation lease renew <ref>`** — extend a timed lease. `--ttl`.

**`federation lease release <ref>`** — give up your own lease.

**`federation lease force-release <ref>`** — admin break of someone else's lease. `--reason`.

**`federation lease steal <ref>`** — force-release and immediately acquire. `--reason`.
```
kata federation lease steal abc4 --reason "laptop offline for a week"
```

**`federation quarantine list`** / **`show <id>`** — inspect push batches the hub rejected.
```
kata federation quarantine list --json
```

**`federation quarantine retry <id>`** — release a quarantined batch without advancing the
push cursor (the same events are resent). `--confirm`, `--reason`.

**`federation quarantine skip <id>`** — drop the batch and move the cursor past it.
`--confirm`, `--reason`.
```
kata federation quarantine skip 7 --confirm 7 --reason "duplicate of the replayed batch"
```

**`sync github enable`** — mirror this project with a GitHub repo. `--repo owner/repo`,
`--host`, `--interval 5m`, `--title-prefix` (default true).
```
kata sync github enable --repo plonkus/kitchen --interval 5m
```

**`sync github once`** / **`status`** / **`disable`** — run one sync now; show state
(`state=not_enabled` when off); turn it off.
```
kata sync github status --agent
```

**`connector list`** — configured connector instances (empty output when there are none).

**`connector status <instance>`** — safe (credential-free) status for one connector.

**`connector field list <instance>`** — public field descriptors you can map.

**`connector field map <instance> <kata-field> --external SELECTOR`** — map a field
bidirectionally. **`connector field unmap <instance> <kata-field>`** disables it.
```
kata connector field map jira-prod priority --external fields.priority.name
```

**`bridge bind <ref>`** — bind one issue to an external root. `--connector`, `--external`,
`--publish-comments`.
```
kata bridge bind abc4 --connector jira-prod --external PROJ-123 --publish-comments
```

**`bridge show <ref>`** — bridge policy and status. **`bridge unbind <ref>`** stops
reconciliation but keeps history.

**`bridge pause <ref>`** (`--reason`) / **`bridge resume <ref>`** — halt and revalidate.

**`bridge reconcile <ref>`** — reconcile now.

**`bridge resolve-comment <ref>`** — settle an uncertain outbound comment: `--retry`,
`--skip`, or `--adopt <external-comment-id>`.

**`bridge resolve-field <ref> <kata-field> --use kata|external`** — settle a field conflict.
```
kata bridge resolve-field abc4 title --use kata
```

## 3. Surprises

Verified against v0.17.2 on this machine.

- **The install/CLI doc URLs move around.** `katatracker.com/start/install/` and
  `/reference/cli/` 404. The live pages are `/docs/get-started/install/` and
  `/docs/reference/cli/`.
- **`kata events --tail` does exist** (SSE stream, NDJSON/agent lines) — and with no
  `--last-event-id` it replays the whole history before going live, so an agent that just
  wants new events must pass a cursor.
- **`delete` needs three things, and the confirm string is project-qualified.** No flags →
  exit 3 `deletion requires --force`. `--force` alone → exit 6 `pass --confirm "DELETE
  claude-kitchen#1xhf"`. The command's own `--help` says `"DELETE <short_id>"`, which is
  wrong; the error message and the website (`<qualified-id>`) are right. `purge` uses the
  unqualified `"PURGE <short_id>"`.
- **`--agent` output is a status line plus indented detail, not one line.** `kata create …
  --agent` prints `OK create 1xhf` then `Issue:`/`Status:` lines; `list` prints `OK list
  count=N` then one `- issue=… ` row each. Parse the `OK <verb>` header for success, not the
  row count. Errors mirror it: `ERR <verb> <kind>: <message>`.
- **`kata ui` prints nothing** — not the URL, not with `--json`, not with `--agent`. It
  exits 0 and opens the browser. The URL comes from `kata daemon status`.
- **Metadata values are JSON.** `meta set abc4 k v` stores `"v"`, and `show`/`meta get`
  print it escaped (`value="\"needs-human\""`). Use `--json-value` for real booleans/numbers.
- **`schedule`/`deadline` are metadata sugar.** Both emit `issue.metadata_updated`, writing
  `scheduled_on` / `deadline_on`. A future `scheduled_on` removes the issue from `ready` and
  `next` but *not* from `list`; a `deadline_on` parks nothing.
- **`search` is lexical by default and `--semantic` fails closed**:
  `mode "semantic" requires [search.embeddings] to be configured`.
- **`kata projects show` takes a positional name.** `--project NAME` is a global flag that
  this command ignores, so `kata projects show --project foo` fails with
  `accepts 1 arg(s), received 0` (exit 2). There is no `kata project` singular.
- **Distinct exit codes**, worth branching on: 2 usage, 3 validation, 4 not found
  (`no .kata.toml ancestor and no git ancestor` outside a bound tree), 6 confirmation
  mismatch, 7 configured remote daemon down (never silently falls back to a local daemon),
  8 `wait` timeout.
- **`kata attention-hook` exists but is hidden** — absent from `kata --help`, documented on
  the site, and it silently no-ops (exit 0, no output) when `KATA_REF` is unset. That silence
  is by design; a hook that never fires is not necessarily broken.
- **The HTTP API is wider than the CLI.** `kata openapi` lists 88 paths including
  `/projects/{id}/recurrences` (recurring tasks), `/issues/{ref}/graph`, project-level
  `/metadata`, and `/ui/*` — none of which have a CLI verb.
- **The daemon auto-starts.** No `daemon start` is needed after install; the first command
  spawns it. `kata daemon locate` will start a local daemon as a side effect of locating it.

## 4. Web UI vs CLI

Verified by opening the UI at the port `kata daemon status` prints
(`http://127.0.0.1:<port>`, which redirects to `/kata`).

**Only in the UI**

- **Recurring tasks.** The issue detail pane has a `RECURRING` section with a `+ New` button
  (the `/recurrences` API). No CLI verb reaches it.
- **The reachable graph.** "Open reachable graph" renders parent/blocks/related as a
  pannable node graph with filters (Full/All/Compact/Follow LR). The CLI can only print links
  one issue at a time.
- **Date-oriented views** — Inbox, Today, Upcoming, Deadlines, Logbook — and a sortable,
  column-configurable table with parent/child expand-collapse. `list`/`ready`/`next` have no
  equivalent of Today or Upcoming.
- **Live updates and a daemon switcher** in the header, so one page can watch several
  daemons.
- **Inline editing**: Markdown description editor, label chips, Scheduled/Due/Owner/Priority
  pickers, comment box.

**Only in the CLI**

- Everything administrative: `projects merge/purge/detach/rewrite-author`, `federation *`,
  `sync github *`, `bridge *`, `connector *`, `tokens *`, `storage postgres *`, `daemon *`,
  `export`/`import`, `openapi`, `mcp serve`.
- Agent primitives: `wait`, `next`, `ready --unowned`, `claim --if-unowned`,
  `events --tail`, `digest`, `audit closes`, `--idempotency-key`, `--if-match`.
- Arbitrary metadata. The UI surfaces labels, schedule and deadline, but there is no
  metadata editor — `work.attention` and friends are CLI-only.
- Destructive verbs: `delete`, `purge`, `restore`.

**The same in both**

The close contract is enforced identically. The UI's "Complete" opens a dialog demanding a
reason radio, an evidence type + value, and a completion note of *at least 40 characters* —
the same discipline as `kata close --reason --evidence --message`. One gap: the UI offers
only Done / Won't do / Duplicate / Superseded; `audit-no-change` is CLI-only.

Two other UI notes: the UI writes events as actor `kata-web`, so `digest` and `audit` can
tell UI activity from agent activity; and the `?issue=` URL parameter wants a full ULID —
a short_id gives "This Kata route is not valid".
