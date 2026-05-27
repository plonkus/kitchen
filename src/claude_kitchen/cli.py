"""CLI entry point for claude-kitchen."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_kitchen.tmux import (
    mc, bare, list_sessions, list_windows, has_session,
    capture_pane, send_keys, tmux, wait_for_prompt,
)
from claude_kitchen.state import (
    state_dir, write_status, read_status, update_status,
    project_slug, wiki_dir, notes_dir,
)
from claude_kitchen.models import max_context_for
from claude_kitchen.spawn import spawn_window, spawn_sous

_PKG_DIR = Path(__file__).parent

# Appended to the role prompt on send_keys delivery (Codex only — Claude
# gets the role via --append-system-prompt-file, which never surfaces as a
# user message). Without this, Codex echoes the role back as if it's a task
# and asks "what should I do?", wasting a turn.
_ROLE_ACK_FOOTER = (
    "\n\n---\n"
    "Acknowledge with one short line (\"Ready, chef.\" or similar), then "
    "wait silently for your ticket. Do NOT ask what to do — the ticket "
    "arrives next.\n"
)

_WIKI_TEMPLATES = {
    "mistakes.md": (
        "# Mistakes\n\n"
        "Lessons learned across kitchens for this project. "
        "When you're burned by something worth remembering across features, append a row.\n\n"
        "| Date | What happened | What to do instead |\n"
        "| --- | --- | --- |\n"
    ),
    "preferences.md": (
        "# Preferences\n\n"
        "Head chef's working style, conventions, pet peeves. "
        "Edit freely.\n"
    ),
}
_NOTES_TEMPLATES = {
    "handoff.md": (
        "# Handoff\n\n"
        "Where we are right now. A fresh sous reads this first to resume.\n"
    ),
    "log.md": (
        "# Log\n\n"
        "Append-only scratch: decisions, attempts, blockers.\n"
    ),
}


def _seed(dir_path: Path, templates: dict[str, str]):
    """Create dir and write any template files that don't already exist."""
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, body in templates.items():
        f = dir_path / name
        if not f.exists():
            f.write_text(body)


def resolve_kitchen(kitchen: str = None) -> str:
    """Resolve which kitchen to target. Returns bare name."""
    if kitchen:
        if kitchen == "projects":
            sys.exit("'projects' is a reserved kitchen name (used for the project wiki).")
        return kitchen
    env = os.environ.get("AGENT_SESSION", "")
    if env:
        return bare(env)
    sessions = list_sessions()
    if len(sessions) == 1:
        return bare(sessions[0])
    if not sessions:
        print("No active kitchens.", file=sys.stderr)
    else:
        print(f"Multiple kitchens active: {', '.join(bare(s) for s in sessions)}", file=sys.stderr)
        print("Use --kitchen <name> to specify.", file=sys.stderr)
    sys.exit(1)


def resolve_project(project: str) -> Path:
    """Resolve project path."""
    p = Path(project).expanduser()
    if p.is_dir():
        return p.resolve()
    sys.exit(f"Project path does not resolve to a directory: {project}")


def create_worktree(project: Path, name: str, worktree_path: Path = None) -> Path:
    """Create a git worktree. Defaults to a sibling directory of the project."""


    git_root = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if git_root.returncode != 0:
        sys.exit(f"Not a git repo: {project}")
    root = Path(git_root.stdout.strip())

    if worktree_path is None:
        worktree_path = root.parent / name

    if worktree_path.exists():
        print(f"Worktree already exists: {worktree_path}", file=sys.stderr)
        return worktree_path

    result = subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", name],
        cwd=str(root),
    )
    if result.returncode != 0:
        sys.exit(f"Failed to create worktree: {name}")

    return worktree_path


def worktree_is_dirty(worktree_path: Path) -> bool:
    """Check if a worktree has uncommitted changes or new commits."""
    # Uncommitted changes
    status = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if status.stdout.strip():
        return True
    # Commits not on any remote-tracking branch
    log = subprocess.run(
        ["git", "-C", str(worktree_path), "log", "--oneline", "@{upstream}..HEAD"],
        capture_output=True, text=True,
    )
    # If upstream doesn't exist (new branch, never pushed), any commits count as dirty
    if log.returncode != 0:
        rev_count = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-list", "--count", "HEAD", "--not", "--remotes"],
            capture_output=True, text=True,
        )
        return rev_count.returncode == 0 and int(rev_count.stdout.strip() or "0") > 0
    return bool(log.stdout.strip())


def remove_worktree(worktree_path: Path, force: bool = False):
    """Remove a git worktree. Prompts if there are uncommitted or unpushed changes."""
    if not force and worktree_is_dirty(worktree_path):
        print(f"Worktree has uncommitted or unpushed changes: {worktree_path}")
        try:
            answer = input("Remove anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "y":
            print("Keeping worktree.")
            return

    git_dir = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if git_dir.returncode == 0:
        common = Path(git_dir.stdout.strip()).resolve()
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(common),
        )
    else:
        print(f"Warning: could not resolve git dir for worktree removal", file=sys.stderr)


def run_hook(hook_dir: Path, hook_name: str, name: str, cwd: Path = None, source: Path = None):
    """Run a <hook_dir>/.kitchen/<hook_name>.sh script if it exists.
    hook_dir: where to find the .kitchen/ folder (typically the source project).
    cwd: working directory for the script (defaults to hook_dir)."""

    hook = hook_dir / ".kitchen" / f"{hook_name}.sh"
    if not hook.exists():
        return
    env = {**os.environ, "KITCHEN_NAME": name}
    if source:
        env["KITCHEN_SOURCE"] = str(source)
    result = subprocess.run(["bash", str(hook)], cwd=str(cwd or hook_dir), env=env)
    if result.returncode != 0:
        print(f"Warning: {hook_name}.sh exited {result.returncode}", file=sys.stderr)


def _sweep_cooks(base: Path, session: str) -> list[str]:
    """Delete cook JSON files whose tmux window no longer exists. Returns names swept."""
    cooks_dir = base / "cooks"
    if not cooks_dir.is_dir():
        return []
    live = set(list_windows(session))
    swept = []
    for f in cooks_dir.glob("*.json"):
        if f.stem not in live:
            f.unlink()
            swept.append(f.stem)
    return swept


def cmd_statusline_segment(args):
    """Print the kitchen-state segment for embedding in a statusline.

    Soft-resolves the current kitchen (AGENT_SESSION env, else single-active
    session). Outside any kitchen → empty output, exit 0, so a wrapper script
    calling this never breaks a user's statusline.

    Stdin (Claude Code session JSON) is ignored — this command is designed to
    be invoked from a wrapper that already consumed stdin. Reading it here
    would deadlock when called with no pipe.
    """
    env = os.environ.get("AGENT_SESSION", "")
    if env:
        kitchen = bare(env)
    else:
        sessions = list_sessions()
        kitchen = bare(sessions[0]) if len(sessions) == 1 else None
    if not kitchen:
        return

    cooks_dir = state_dir(kitchen) / "cooks"
    total = active = 0
    if cooks_dir.is_dir():
        for f in cooks_dir.glob("*.json"):
            total += 1
            try:
                if json.loads(f.read_text()).get("status") in ("working", "booting"):
                    active += 1
            except (json.JSONDecodeError, OSError):
                pass

    segments = []
    if env:
        segments.append(f"[ tmux attach -t {env} ]")
    segments.append(f"[ {active}/{total} agents active ]")
    print("  ".join(segments))


def cmd_sweep(args):
    kitchen = resolve_kitchen(args.kitchen)
    swept = _sweep_cooks(state_dir(kitchen), mc(kitchen))
    if swept:
        print(f"Swept {len(swept)} stale cook(s): {', '.join(sorted(swept))}")
    else:
        print("Swept 0 stale cooks.")


def cmd_open(args):
    project = resolve_project(args.project)
    name = args.name or project.name
    session = mc(name)
    base = state_dir(name)

    kitchen_file = base / "kitchen.json"
    resuming = kitchen_file.exists()

    sous_session_id = None
    if args.resume:
        if not resuming:
            sys.exit(f"--resume: no existing kitchen \"{name}\" to resume.")
        sous_session_id = json.loads(kitchen_file.read_text()).get("sous_session_id")
        if not sous_session_id:
            sys.exit(
                f"--resume: no sous session recorded for \"{name}\" yet. "
                f"Open the kitchen normally once so the sous can save its session id."
            )

    if resuming:
        kj = json.loads(kitchen_file.read_text())
        project = Path(kj.get("worktree", kj.get("source", str(project))))
        derived = project_slug(Path(kj["source"]))
        if kj.get("slug") and kj["slug"] != derived:
            sys.exit(
                f"Stored slug ({kj['slug']}) does not match current git remote "
                f"({derived}). Run `kitchen close {name}` and reopen."
            )
        kj["slug"] = derived
        kitchen_file.write_text(json.dumps(kj) + "\n")
        slug = derived
    else:
        base.mkdir(parents=True, exist_ok=True)
        source = project
        slug = project_slug(source)

        kj = {"source": str(source), "slug": slug}
        if args.name:
            wt_path = Path(args.worktree_path) if args.worktree_path else None
            project = create_worktree(project, name, wt_path)
            kj["worktree"] = str(project)

        kitchen_file.write_text(json.dumps(kj) + "\n")
        run_hook(source, "on-open", name, cwd=project, source=source)

    _seed(wiki_dir(slug), _WIKI_TEMPLATES)
    _seed(notes_dir(name), _NOTES_TEMPLATES)

    if not has_session(session):
        tmux("new-session", "-d", "-s", session, "-n", "_placeholder")
    _sweep_cooks(base, session)
    mcp_config = {
        "mcpServers": {
            "kitchen": {
                "command": "kitchen",
                "args": ["channel-server", name],
            }
        }
    }
    (base / ".mcp.json").write_text(json.dumps(mcp_config, indent=2) + "\n")

    if resuming:
        print(f"Kitchen \"{name}\" — sous chef back on the line.")
    else:
        print(f"Kitchen \"{name}\" is open. Sous chef on the line.")
    print(f"   tmux attach -t {session}")

    sous_md = _PKG_DIR / "sous-chef.md"
    if not sous_md.exists():
        sys.exit(f"sous-chef.md not found at {sous_md}")

    spawn_sous(name, base, sous_md.read_text(), project, slug=slug,
               resume_session_id=sous_session_id)


def cmd_hire(args):
    kitchen = resolve_kitchen(args.kitchen)
    session = mc(kitchen)
    base = state_dir(kitchen)
    name = args.name
    backend = args.backend
    cwd = args.project or os.getcwd()
    cwd = str(resolve_project(cwd))

    role = getattr(args, "role", None) or "_default"
    role_path = _PKG_DIR / "roles" / f"{role}.md"
    if not role_path.exists():
        valid = sorted(p.stem for p in (_PKG_DIR / "roles").glob("*.md"))
        sys.exit(f"Unknown role '{role}'. Valid roles: {', '.join(valid)}")

    # Claude takes the role as a system prompt file; Codex gets it as a
    # first message via send_keys after the prompt appears.
    role_to_pass = role_path if backend == "claude" else None

    write_status(base, name, {"status": "booting", "agent": name, "backend": backend})

    effort = getattr(args, "effort", None)
    ok = spawn_window(
        session=session, name=name, cwd=cwd,
        backend=backend, status_dir=str(base),
        effort=effort, role_path=role_to_pass,
    )
    if not ok:
        # update_status preserves durable fields (the booting write above
        # set `backend`); benign at hire-time but enforces the §Status
        # preservation invariant by code rather than happenstance.
        update_status(base, name, status="failed")
        print(f"Failed to boot {name}.", file=sys.stderr)
        sys.exit(1)

    # Keep this barrier: the sous will call `kitchen ticket <cook>` immediately
    # after hire returns. Without it, the first ticket lands in a booting pane
    # and is lost. Also required for the codex send_keys role delivery below.
    if not wait_for_prompt(session, name, backend):
        update_status(base, name, status="failed")
        sys.exit(f"{name} didn't show prompt within timeout.")

    if backend == "codex":
        send_keys(session, name, role_path.read_text() + _ROLE_ACK_FOOTER,
                  backend=backend, log_dir=base / "cooks")

    print(f"{name} is on the station. Yes, chef!")


def cmd_ticket(args):
    kitchen = resolve_kitchen(args.kitchen)
    session = mc(kitchen)
    base = state_dir(kitchen)
    name = args.cook
    message = args.message

    backend = (read_status(base, name) or {}).get("backend")
    send_keys(session, name, message, backend=backend, log_dir=base / "cooks")

    # Mark cook as working. Codex has no UserPromptSubmit-equivalent hook
    # event (Codex `notify` only fires on completion), so for Codex cooks
    # this is the only "cook started working" signal. For Claude cooks the
    # hook also writes status="working" — both writes are idempotent and
    # whichever lands first closes the race. update_status preserves
    # durable fields like `tokens` and `backend`.
    update_status(base, name, status="working")
    print(f"Ticket fired to {name}. Heard!")


def cmd_peek(args):
    kitchen = resolve_kitchen(args.kitchen)
    session = mc(kitchen)
    content = capture_pane(session, args.cook, full=args.full)
    if content:
        print(content)
    else:
        print("(screen is empty)")


def cmd_roles(args):
    """List available roles with their first-line descriptions."""
    roles_dir = _PKG_DIR / "roles"
    files = sorted(roles_dir.glob("*.md"))
    if not files:
        print("(no roles found)")
        return
    for f in files:
        content = f.read_text()
        first = content.splitlines()[0] if content else ""
        desc = first.lstrip("# ").strip()
        print(f"  {f.stem:12s}  {desc}")


def cmd_brigade(kitchen: str = None):
    sessions = list_sessions()
    if kitchen:
        sessions = [s for s in sessions if bare(s) == kitchen]

    if not sessions:
        print("No active kitchens.")
        return

    for session in sorted(sessions):
        name = bare(session)
        base = state_dir(name)
        windows = list_windows(session)

        print(f"kitchen: {name}")

        # One row per cook: <name>  <status>  <ctx>. Name column padded to
        # the longest name in this kitchen's listing; status padded to a
        # fixed width so ctx aligns vertically. Summary intentionally
        # dropped — sous gets summaries via the channel push (Chunk 3),
        # so brigade duplicating them is waste.
        name_w = max((len(w) for w in windows), default=0)
        for win in windows:
            status = read_status(base, win) or {}
            s = status.get("status", "unknown")
            ctx = _format_tokens(status.get("tokens"))
            print(f"  {win:<{name_w}}  {s:<7}  {ctx}")


def cmd_clock_out(args):
    kitchen = resolve_kitchen(args.kitchen)
    session = mc(kitchen)
    tmux("kill-window", "-t", f"{session}:{args.cook}")
    # Clean up status file
    base = state_dir(kitchen)
    cook_file = base / "cooks" / f"{args.cook}.json"
    cook_file.unlink(missing_ok=True)
    print(f"{args.cook} has clocked out.")


def _hook_gate() -> tuple[str, str, Path] | None:
    """Check env vars required for hooks. Returns (name, session, status_dir) or None."""
    name = os.environ.get("AGENT_NAME", "")
    session = os.environ.get("AGENT_SESSION", "")
    status = os.environ.get("STATUS_DIR", "")
    if not (name and session and status):
        return None
    return name, session, Path(status)


def cmd_hook(args):
    """Handle hook events from Claude Code (stdin) or Codex (--message arg)."""
    ctx = _hook_gate()
    if not ctx:
        return
    name, session, base = ctx

    codex = args.command == "hook-codex"
    try:
        raw = args.json_payload if codex else sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        return

    event = payload.get("hook_event_name", "")

    if name == "sous":
        # Sous hook is otherwise a no-op — prevents echo loops where the sous's
        # own Stop events would feed back through the cook channel push below.
        # Carveout: capture session_id on Claude Stop so `kitchen open --resume`
        # can find this exact sous after a crash. (Sous is always Claude.)
        if not codex and event == "Stop":
            sid = payload.get("session_id")
            kj_file = base / "kitchen.json"
            if sid and kj_file.exists():
                kj = json.loads(kj_file.read_text())
                if kj.get("sous_session_id") != sid:
                    kj["sous_session_id"] = sid
                    kj_file.write_text(json.dumps(kj) + "\n")
        return

    if not codex:
        # Claude branch dispatches by event type. UserPromptSubmit is the
        # canonical "cook started working" trigger — closes the cmd_ticket
        # race where status="working" was only written after send_keys
        # returned. Must NOT read last_assistant_message (not present on
        # this event) and must NOT call send_to_socket (not a completion).
        # update_status (not write_status) preserves durable fields like
        # `tokens` and `backend` across this non-completion transition.
        if event == "UserPromptSubmit":
            update_status(
                base, name,
                agent=name, session=session, status="working",
            )
            return
        if event != "Stop":
            return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if codex:
        summary = payload.get("last-assistant-message", "")
    else:
        try:
            summary = payload["last_assistant_message"]
        except KeyError:
            # Dump a sanitized payload snapshot so we can diagnose without
            # leaking env. NO env dump — previous instrumentation (commit
            # 1a9b42f on hook-trace-instrumentation) leaked dict(os.environ).
            dump = {
                "payload_keys": sorted(payload.keys()),
                "event_name": event,
                "kitchen": base.name,
                "raw_stdin": raw,
            }
            dump_path = Path("/tmp") / f"kitchen-hook-fail-{int(datetime.now(timezone.utc).timestamp())}-{os.getpid()}.json"
            dump_path.write_text(json.dumps(dump, indent=2, default=str))
            raise

    updates = {
        "agent": name, "session": session, "status": "idle",
        "ts": ts, "summary": summary,
    }
    if codex:
        thread_id = payload.get("thread-id", "")
        updates["session_id"] = thread_id
        # Codex doesn't carry usage in the notify payload — read it from
        # the cook's rollout JSONL at ~/.codex/sessions/YYYY/MM/DD/
        # rollout-<ts>-<thread_id>.jsonl. See spec §Source of truth (Codex).
        tokens = _codex_tokens_from_rollout(thread_id)
        if tokens is not None:
            updates["tokens"] = tokens
    else:
        # The Claude Stop hook payload carries session_id and transcript_path
        # but no usage data — empirically verified 2026-04-29 against Claude
        # Code v2.1.119. Read usage from the transcript JSONL the payload
        # points at; the most recent assistant line carries message.usage.
        updates["session_id"] = payload.get("session_id", "")
        tokens = _claude_tokens_from_transcript(payload.get("transcript_path"))
        if tokens is not None:
            updates["tokens"] = tokens

    # update_status (not write_status) preserves `tokens` and `backend` if
    # this Stop write didn't compute them (e.g. transcript missing).
    update_status(base, name, **updates)

    # Read back so the channel push reflects the merged tokens — covers
    # the transcript-miss case where `updates` skipped tokens but prior
    # tokens survived via update_status.
    push = {"cook": name, "summary": summary, "ts": ts}
    final = read_status(base, name) or {}
    ctx = _ctx_for_channel(final.get("tokens"))
    if ctx:
        push["ctx"] = ctx

    from claude_kitchen.channel import send_to_socket, SOCK_NAME
    send_to_socket(base / SOCK_NAME, push)


def _claude_tokens_from_transcript(transcript_path):
    """Return {input, max} from the most recent assistant message in the
    transcript JSONL. None if the file is missing or has no assistant
    message yet (newly-spawned cook before its first turn)."""
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.exists():
        return None
    last = None
    with p.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "assistant":
                last = obj["message"]
    if not last:
        return None
    usage = last.get("usage") or {}
    # Spec §What to store: cache hits dominate cached-prompt sessions;
    # ignoring them would dramatically undercount. Output tokens are not
    # context and are excluded.
    input_total = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    return {"input": input_total, "max": max_context_for(last.get("model"))}


_CODEX_SCAN_BACK_DAYS = 7


def _codex_tokens_from_rollout(thread_id):
    """Find the cook's Codex rollout JSONL (matched on `thread-id` from the
    notify payload) and extract tokens from the most recent token_count
    event with populated `info`. Returns {input, max} or None if the file
    isn't found within the 7-day scan-back window or has no usable line.

    Scan order: today, then walk back day-by-day. Codex writes to
    YYYY/MM/DD/ in UTC; sessions can span midnight, hence the walk."""
    if not thread_id:
        return None
    base = Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        return None
    suffix = f"-{thread_id}.jsonl"
    today = datetime.now(timezone.utc).date()
    for delta in range(_CODEX_SCAN_BACK_DAYS + 1):
        day = today - timedelta(days=delta)
        day_dir = base / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if not day_dir.is_dir():
            continue
        for f in day_dir.iterdir():
            if f.name.startswith("rollout-") and f.name.endswith(suffix):
                return _read_codex_token_count(f)
    return None


def _read_codex_token_count(rollout_path):
    """Walk the rollout JSONL; return the most recent token_count event's
    {input, max} or None.

    The `input_tokens` we want is `info.last_token_usage.input_tokens` —
    the size of the current turn's API call, i.e. the live context window
    occupancy. NOT `total_token_usage.input_tokens`, which is the
    cumulative running total across every API call in the session and
    can exceed the model's context window for long sessions.

    Codex issue #14489 (last_token_usage re-emitted unchanged on
    rate-limit-only events) is handled by the `info: null` skip below —
    those events emit token_count with no `info` and are filtered out
    before they can shadow a real measurement."""
    latest = None
    with rollout_path.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "event_msg":
                continue
            payload = (obj.get("payload") or {})
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if info:
                latest = info
    if not latest:
        return None
    last = latest.get("last_token_usage") or {}
    return {
        "input": last.get("input_tokens", 0),
        "max": latest.get("model_context_window"),
    }


def _format_tokens(tokens):
    """Render the brigade tokens column per spec §Display.

    Three cases:
      - tokens absent or no .input    →  "—"
      - .input present, .max null     →  "<input_k>k (unknown)"
      - both present                  →  "<pct>% (<input_k>k/<max_k>k)"
    Floor rounding throughout."""
    return _ctx_for_channel(tokens) or "—"


def _ctx_for_channel(tokens):
    """Format tokens for the channel `ctx` attribute. Returns None when
    tokens should be omitted entirely from the channel push (no input
    known); otherwise returns the same string brigade renders."""
    if not tokens:
        return None
    inp = tokens.get("input")
    if inp is None:
        return None
    inp_k = inp // 1000
    mx = tokens.get("max")
    if mx is None or mx == 0:
        return f"{inp_k}k (unknown)"
    pct = (inp * 100) // mx
    mx_k = mx // 1000
    return f"{pct}% ({inp_k}k/{mx_k}k)"


def cmd_setup(args):
    """Check hook installation and skill status. Print what's needed."""
    all_good = True
    claude_hook_cmd = "kitchen hook"
    codex_hook_cmd = "kitchen"

    # --- Claude Code hooks ---
    claude_settings = Path.home() / ".claude" / "settings.json"
    if claude_settings.exists() and claude_hook_cmd in claude_settings.read_text():
        print("✅ Claude hooks installed")
    else:
        all_good = False
        stop_entry = json.dumps(
            {"hooks": [{"type": "command", "command": claude_hook_cmd}]},
            indent=2,
        )
        prompt_entry = json.dumps(
            {"matcher": "", "hooks": [{"type": "command", "command": claude_hook_cmd}]},
            indent=2,
        )
        print(f"❌ Claude hooks not found in {claude_settings}")
        print(f"   Add to the \"hooks\" key in {claude_settings}:")
        print(f'   "Stop": [{stop_entry}],')
        print(f'   "UserPromptSubmit": [{prompt_entry}]')
        print()

    # --- Codex hook ---
    # Codex deprecated [features].codex_hooks in favor of [features].hooks.
    # Accept either form so existing setups stay green; soft-warn on the old one.
    codex_config = Path.home() / ".codex" / "config.toml"
    codex_content = codex_config.read_text() if codex_config.exists() else ""
    has_new = re.search(r"\bhooks\s*=\s*true", codex_content) is not None
    has_old = re.search(r"\bcodex_hooks\s*=\s*true", codex_content) is not None
    if 'notify = ["kitchen", "hook-codex"]' in codex_content:
        print("✅ Codex hook installed")
        if has_old and not has_new:
            print("⚠️  [features].codex_hooks is deprecated by Codex. Change to: hooks = true")
    else:
        all_good = False
        print(f"❌ Codex hook not found in {codex_config}")
        print(f'   Add at the top level:')
        print(f'   notify = ["kitchen", "hook-codex"]')
        if not has_new and not has_old:
            print(f"   And under [features]:")
            print(f"   hooks = true")
        print()

    # --- Skill symlink (auto-install/update) ---
    repo_root = _PKG_DIR
    while repo_root != repo_root.parent and not (repo_root / "pyproject.toml").exists():
        repo_root = repo_root.parent
    skill_source = repo_root / "skill"
    skill_target = Path.home() / ".claude" / "skills" / "claude-kitchen"
    if skill_target.is_symlink() and skill_target.resolve() == skill_source.resolve():
        print("✅ Skill installed")
    else:
        skill_target.parent.mkdir(parents=True, exist_ok=True)
        if skill_target.exists() or skill_target.is_symlink():
            skill_target.unlink() if skill_target.is_symlink() else shutil.rmtree(skill_target)
        skill_target.symlink_to(skill_source)
        print("✅ Skill installed (created symlink)")
        print()

    # --- mcp SDK ---
    try:
        import mcp  # noqa: F401
        print("✅ mcp SDK available")
    except ImportError:
        all_good = False
        print("❌ mcp SDK not installed")
        print("   Run: uv tool install --editable .")
        print()

    # --- superpowers plugin ---
    # Canonical install path for marketplace plugins (see Claude Code plugin docs):
    #   ~/.claude/plugins/cache/<marketplace-name>/<plugin-name>/
    sp_path = Path.home() / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers"
    if sp_path.exists():
        print(f"✅ superpowers plugin installed ({sp_path})")
    else:
        all_good = False
        print("❌ superpowers plugin not found")
        print(f"   Expected at: {sp_path}")
        print("   Install via Claude Code: /plugin install superpowers from superpowers-marketplace")
        print()

    # --- Claude Code version (channels need >= 2.1.80) ---
    cv = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    if cv.returncode != 0:
        all_good = False
        print("❌ `claude` CLI not on PATH")
        print()
    else:
        ver = cv.stdout.strip().split()[0]
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", ver)
        if not m:
            all_good = False
            print(f"❌ Could not parse Claude CLI version: {ver!r}")
            print()
        else:
            major, minor, patch = map(int, m.groups())
            if (major, minor, patch) >= (2, 1, 80):
                print(f"✅ Claude CLI {ver} (channels supported)")
            else:
                all_good = False
                print(f"❌ Claude CLI {ver} too old; need >= 2.1.80 for channels")
                print()
        print("ℹ️  Channels require claude.ai auth (not Console / API key). "
              "If `kitchen open` shows channel errors, run `claude /login`.")

    # --- Statusline (advisory; kitchen works without it) ---
    # Three states: (1) no/broken statusLine → install advice; (2) user has
    # their own statusline, no kitchen segment → embed advice; (3) kitchen
    # segment present (either directly as `kitchen statusline-segment` or
    # referenced inside a wrapper script) → green.
    import shlex
    statusline_pkg = _PKG_DIR / "statusline-command.sh"
    statusline_install = Path.home() / ".claude" / "statusline-command.sh"

    def _cmd_path_tokens(cmd: str) -> list[Path]:
        return [Path(t).expanduser() for t in shlex.split(cmd)]

    def _has_kitchen_segment(cmd: str) -> bool:
        # Direct CLI invocation: `kitchen statusline-segment` anywhere.
        tokens = shlex.split(cmd)
        if "statusline-segment" in tokens:
            return True
        # Wrapper script whose text references the segment call.
        for p in _cmd_path_tokens(cmd):
            if p.is_file():
                try:
                    if "statusline-segment" in p.read_text():
                        return True
                except (OSError, UnicodeDecodeError):
                    continue
        return False

    cmd = ""
    sl_file_resolves = False
    if claude_settings.exists():
        try:
            sl = json.loads(claude_settings.read_text()).get("statusLine") or {}
            cmd = sl.get("command", "")
            sl_file_resolves = bool(cmd) and (
                "statusline-segment" in shlex.split(cmd)
                or any(p.exists() for p in _cmd_path_tokens(cmd))
            )
        except json.JSONDecodeError:
            pass

    if cmd and sl_file_resolves and _has_kitchen_segment(cmd):
        print("✅ Statusline configured (kitchen segment active)")
    elif cmd and sl_file_resolves:
        # Don't overwrite — advise embedding.
        print("⚠️  Statusline configured, but no kitchen segment detected")
        print(f'   Add `$(kitchen statusline-segment)` to your existing statusline')
        print(f"   wherever you want cook/attach info to appear. Example line:")
        print(f'     printf "%s  %s\\n" "<your existing output>" "$(kitchen statusline-segment)"')
        print()
    else:
        minimal = json.dumps(
            {"type": "command", "command": "kitchen statusline-segment"},
            indent=2,
        )
        richer = json.dumps(
            {"type": "command", "command": str(statusline_install)},
            indent=2,
        )
        print("⚠️  Statusline not configured (optional — kitchen works without it)")
        print(f"   Option 1 (minimal): add to {claude_settings}:")
        print(f'     "statusLine": {minimal}')
        print(f"   Option 2 (richer — context/model/branch + kitchen segment):")
        print(f"     Copy {statusline_pkg}")
        print(f"     to   {statusline_install}  (chmod +x after copy)")
        print(f"     then add to {claude_settings}:")
        print(f'     "statusLine": {richer}')
        print()

    # --- legacy 'projects' kitchen collision ---
    legacy = Path.home() / ".claude-kitchen" / "projects" / "kitchen.json"
    if legacy.exists():
        all_good = False
        print(f"❌ Legacy kitchen named 'projects' exists at {legacy.parent}")
        print(f"   This name is now reserved for the project wiki root.")
        print(f"   Rename it: mv {legacy.parent} {legacy.parent}-legacy")
        print()

    if all_good:
        print("\n🍳 All set. Kitchen is ready.")
        print(f"💡 Customize workflow defaults at: {_PKG_DIR / 'sous-chef.md'} and {_PKG_DIR / 'roles'}/")
    else:
        print("\n🔧 Fix the items above, then run `kitchen setup` again.")
        sys.exit(1)


def cmd_close(args):
    kitchen = resolve_kitchen(args.kitchen)
    session = mc(kitchen)
    base = state_dir(kitchen)

    # Run on-close hook and remove worktree BEFORE killing tmux
    kitchen_file = base / "kitchen.json"
    if kitchen_file.exists():
        try:
            kj = json.loads(kitchen_file.read_text())
            source = Path(kj["source"])
            cwd = Path(kj.get("worktree", kj["source"]))
            if cwd.exists():
                run_hook(source, "on-close", kitchen, cwd=cwd)
            if "worktree" in kj and not args.keep_worktree:
                worktree = Path(kj["worktree"])
                if worktree.exists():
                    remove_worktree(worktree, force=args.force)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: bad kitchen.json: {e}", file=sys.stderr)
        kitchen_file.unlink(missing_ok=True)

    tmux("kill-session", "-t", session)

    # Clean up mcp config, socket, pid, and stale cook state
    for f in (".mcp.json", "kitchen.sock", "sous.pid"):
        (base / f).unlink(missing_ok=True)
    cooks_dir = base / "cooks"
    if cooks_dir.is_dir():
        shutil.rmtree(cooks_dir)
    notes = base / "notes"
    if notes.is_dir():
        shutil.rmtree(notes)

    print("Kitchen closed. Service over.")


def main():
    parser = argparse.ArgumentParser(prog="kitchen", description="claude-kitchen CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="Open a kitchen")
    p_open.add_argument("name", nargs="?", default=None, help="Kitchen/worktree name (omit to use cwd without worktree)")
    p_open.add_argument("project", nargs="?", default=".", help="Project path or name (default: cwd)")
    p_open.add_argument("--worktree-path", help="Custom path for the worktree (default: sibling directory)")
    p_open.add_argument("--resume", action="store_true", help="Resume the previous sous conversation (uses sous_session_id from kitchen.json)")

    p_hire = sub.add_parser("hire", help="Hire a cook")
    p_hire.add_argument("name", help="Cook name")
    p_hire.add_argument("--backend", default="claude", choices=["claude", "codex"])
    p_hire.add_argument("--kitchen", help="Target kitchen")
    p_hire.add_argument("--project", help="Project path (defaults to cwd)")
    p_hire.add_argument("--role", help="Role from src/claude_kitchen/roles/ (Claude only)")
    p_hire.add_argument("--effort", help="Reasoning effort (e.g. low, medium, high, max)")

    p_ticket = sub.add_parser("ticket", help="Send a ticket to a cook")
    p_ticket.add_argument("cook", help="Cook name")
    p_ticket.add_argument("message", help="Message to send")
    p_ticket.add_argument("--kitchen", help="Target kitchen")

    p_peek = sub.add_parser("peek", help="Peek at a cook's screen")
    p_peek.add_argument("cook", help="Cook name")
    p_peek.add_argument("--full", action="store_true", help="Full scrollback")
    p_peek.add_argument("--kitchen", help="Target kitchen")

    p_brigade = sub.add_parser("brigade", help="Show kitchen status")
    p_brigade.add_argument("kitchen", nargs="?", help="Kitchen name (omit for all)")

    p_clock = sub.add_parser("clock-out", help="Clock out a cook (hard kill)")
    p_clock.add_argument("cook", help="Cook name")
    p_clock.add_argument("--kitchen", help="Target kitchen")

    p_sweep = sub.add_parser("sweep", help="Delete stale cook state files (orphans)")
    p_sweep.add_argument("--kitchen", help="Target kitchen")

    p_close = sub.add_parser("close", help="Close a kitchen")
    p_close.add_argument("kitchen", nargs="?", help="Kitchen name")
    p_close.add_argument("--force", action="store_true", help="Remove worktree even with unpushed changes")
    p_close.add_argument("--keep-worktree", action="store_true", help="Skip worktree removal entirely")

    sub.add_parser("hook", help="Claude Code hook handler (called by hooks, not directly)")

    p_hook_codex = sub.add_parser("hook-codex", help="Codex hook handler (called by notify, not directly)")
    p_hook_codex.add_argument("json_payload", nargs="?", default="{}", help="JSON from Codex")

    sub.add_parser("setup", help="Check hook installation status")

    sub.add_parser(
        "statusline-segment",
        help="Print the kitchen-state segment for embedding in a statusline",
    )

    sub.add_parser("roles", help="List available cook roles")

    p_channel = sub.add_parser("channel-server", help="(internal) Run channel MCP server")
    p_channel.add_argument("kitchen", help="Kitchen name")

    args = parser.parse_args()

    if args.command == "open":
        cmd_open(args)
    elif args.command == "hire":
        cmd_hire(args)
    elif args.command == "ticket":
        cmd_ticket(args)
    elif args.command == "peek":
        cmd_peek(args)
    elif args.command == "brigade":
        cmd_brigade(kitchen=args.kitchen)
    elif args.command == "clock-out":
        cmd_clock_out(args)
    elif args.command == "sweep":
        cmd_sweep(args)
    elif args.command == "close":
        cmd_close(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "statusline-segment":
        cmd_statusline_segment(args)
    elif args.command == "roles":
        cmd_roles(args)
    elif args.command in ("hook", "hook-codex"):
        cmd_hook(args)
    elif args.command == "channel-server":
        from claude_kitchen.channel import main as channel_main
        channel_main(args.kitchen)
