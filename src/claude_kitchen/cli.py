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
    list_kitchens, list_windows, has_session, target,
    capture_pane, send_keys, tmux, wait_for_prompt, PromptBlocked, kill_server, pane_busy, attach_cmd,
    PROBE_TIMEOUT, SESSION,
)
from claude_kitchen.state import (
    state_dir, write_status, read_status, update_status,
    project_slug, namespaced, wiki_dir, notes_dir, channel_servers, atomic_write_json,
    MCP_CONFIG_NAME, LEGACY_MCP_CONFIG_NAME,
)
from claude_kitchen.models import max_context_for
from claude_kitchen.spawn import (spawn_window, spawn_sous,
                                  check_sous_pid, CLAUDE_MODELS, CODEX_MODELS,
                                  SUPERPOWERS_MARKETPLACES)

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


def _cwd_project() -> Path | None:
    """The git toplevel of cwd, or None when cwd isn't inside a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def resolve_kitchen(kitchen: str = None) -> str:
    """Resolve which kitchen to target. Returns bare name."""
    if kitchen:
        if kitchen == "projects":
            sys.exit("'projects' is a reserved kitchen name (used for the project wiki).")
        # A bare name typed from inside a project root still resolves to its
        # namespaced kitchen when no literal `ck-<kitchen>` session exists —
        # so `kitchen close foo` reaches `ck-<slug>-foo`. If the bare session
        # does exist (a legacy kitchen) we keep targeting it.
        if not has_session(kitchen):
            project = _cwd_project()
            if project and has_session(namespaced(project, kitchen)):
                return namespaced(project, kitchen)
        return kitchen
    env = os.environ.get("AGENT_KITCHEN", "")
    if env:
        return env
    kitchens = list_kitchens()
    if len(kitchens) == 1:
        return kitchens[0]
    if not kitchens:
        print("No active kitchens.", file=sys.stderr)
    else:
        print(f"Multiple kitchens active: {', '.join(kitchens)}", file=sys.stderr)
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


def _sweep_cooks(base: Path, kitchen: str) -> list[str]:
    """Delete cook JSON files whose tmux window no longer exists. Returns names swept."""
    cooks_dir = base / "cooks"
    if not cooks_dir.is_dir():
        return []
    live = set(list_windows(kitchen))
    swept = []
    for f in cooks_dir.glob("*.json"):
        if f.stem not in live:
            f.unlink()
            swept.append(f.stem)
    return swept


def cmd_statusline_segment(args):
    """Print the kitchen-state segment for embedding in a statusline.

    Soft-resolves the current kitchen (AGENT_KITCHEN env, else the single live
    kitchen). Outside any kitchen → empty output, exit 0, so a wrapper script
    calling this never breaks a user's statusline.

    Stdin (Claude Code session JSON) is ignored — this command is designed to
    be invoked from a wrapper that already consumed stdin. Reading it here
    would deadlock when called with no pipe.
    """
    env = os.environ.get("AGENT_KITCHEN", "")
    if env:
        kitchen = env
    else:
        kitchens = list_kitchens()
        kitchen = kitchens[0] if len(kitchens) == 1 else None
    if not kitchen:
        return

    # A statusline is wired into the user's prompt — it must NEVER raise, and it
    # must never make the head chef WAIT. A stale AGENT_KITCHEN (kitchen already
    # torn down) renders nothing rather than an attach hint to a kitchen that's
    # gone.
    #
    # Both tmux calls below carry PROBE_TIMEOUT rather than the default 15s.
    # This kitchen now has its own tmux server, so "my server is wedged" is a
    # first-class state — and at the default budget a wedged server would freeze
    # the prompt for 15 seconds on EVERY render. Bounded at PROBE_TIMEOUT the
    # segment simply goes quiet, which is the same thing it does for a kitchen
    # that is down.
    if not has_session(kitchen, timeout=PROBE_TIMEOUT):
        return

    # Count live tmux windows, not cooks/*.json files. State files for cooks
    # whose window is gone linger until the next `kitchen open`/`sweep`, so a
    # raw glob over-counts by every orphan — which is how a 9-cook kitchen
    # rendered "5/18". list_windows is the same live-window source brigade
    # uses, so the statusline count now matches brigade and describes exactly
    # the kitchen the attach target points at.
    base = state_dir(kitchen)
    total = active = 0
    try:
        windows = list_windows(kitchen, timeout=PROBE_TIMEOUT)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # CalledProcessError: session vanished between has_session and the
        # listing. TimeoutExpired: this kitchen's server answered the probe and
        # then stopped answering — possible now that each kitchen has its own
        # server, and it must degrade to a quiet segment, not a traceback in the
        # head chef's prompt.
        windows = []
    for win in windows:
        total += 1
        try:
            if (read_status(base, win) or {}).get("status") in ("working", "booting"):
                active += 1
        except (json.JSONDecodeError, OSError):
            pass  # malformed/unreadable cook file → count it inactive, not fatal

    segments = []
    if env:
        # Full `-L` form: the kitchen has its own tmux socket, so the bare
        # `tmux attach -t <session>` this used to print no longer finds it. No
        # `-t`: the kitchen's server holds exactly one session.
        segments.append(f"[ {attach_cmd(kitchen)} ]")
    segments.append(f"[ {active}/{total} agents active ]")
    print("  ".join(segments))


def cmd_sweep(args):
    kitchen = resolve_kitchen(args.kitchen)
    swept = _sweep_cooks(state_dir(kitchen), kitchen)
    if swept:
        print(f"Swept {len(swept)} stale cook(s): {', '.join(sorted(swept))}")
    else:
        print("Swept 0 stale cooks.")


def _legacy_bare_kitchen(requested: str, project: Path):
    """Detect a pre-namespacing bare-name kitchen owned by this project.

    Returns (name, base, kitchen_file) when a kitchen at the bare
    `requested` name has both a live `ck-<requested>` session and a
    state dir whose recorded `source` slugs to this project's slug —
    i.e. it's this project's own legacy kitchen. Otherwise None.
    """
    base = state_dir(requested)
    kitchen_file = base / "kitchen.json"
    if not kitchen_file.exists() or not has_session(requested):
        return None
    try:
        src = json.loads(kitchen_file.read_text()).get("source")
    except (json.JSONDecodeError, OSError):
        return None
    if not src or not Path(src).is_dir():
        return None
    if project_slug(Path(src)) != project_slug(project):
        return None
    return requested, base, kitchen_file


def cmd_open(args):
    project = resolve_project(args.project)
    requested = args.name or project.name
    # Namespace the kitchen by project slug so kitchens for different projects
    # never collide on tmux session / state dir / socket names — e.g.
    # `kitchen open main` in two repos yields my-project-main and other-repo-main,
    # not a shared "main". The git branch/worktree keeps the bare `requested`
    # name (it already lives inside the project's own repo).
    name = namespaced(project, requested)
    base = state_dir(name)
    kitchen_file = base / "kitchen.json"

    # Soft cutover: a kitchen created before namespacing lives at the bare
    # `requested` name. If the namespaced form doesn't exist yet but a
    # bare-form kitchen owned by this same project does, attach to it
    # instead of forking a fresh namespaced kitchen alongside the live one.
    legacy = not kitchen_file.exists() and _legacy_bare_kitchen(requested, project)
    if legacy:
        name, base, kitchen_file = legacy
        print(
            f"kitchen '{requested}' predates namespacing; consider close+reopen "
            f"as '{namespaced(project, requested)}' to align with the new convention."
        )

    # Before ANY mutation. Everything below this line writes: kitchen.json, the
    # MCP config, the tmux session. A duplicate open must be a no-op, not a
    # half-applied one that aborts at the end.
    check_sous_pid(base)

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
        # A legacy bare kitchen stores its old long-form slug; the slug
        # format changed under it, so refresh rather than treating the
        # format change as remote drift (detection already confirmed it's
        # the same repo).
        if not legacy and kj.get("slug") and kj["slug"] != derived:
            sys.exit(
                f"Stored slug ({kj['slug']}) does not match current git remote "
                f"({derived}). Run `kitchen close {name}` and reopen."
            )
        kj["slug"] = derived
        kitchen_file.write_text(json.dumps(kj) + "\n")
        slug = derived
    else:
        source = project
        slug = project_slug(source)

        kj = {"source": str(source), "slug": slug}
        if args.name:
            wt_path = Path(args.worktree_path) if args.worktree_path else None
            project = create_worktree(project, requested, wt_path)
            kj["worktree"] = str(project)

        # After create_worktree, so a named open refused outside git leaves no
        # empty state dir behind.
        base.mkdir(parents=True, exist_ok=True)
        kitchen_file.write_text(json.dumps(kj) + "\n")
        run_hook(source, "on-open", name, cwd=project, source=source)

    _seed(wiki_dir(slug), _WIKI_TEMPLATES)
    _seed(notes_dir(name), _NOTES_TEMPLATES)

    # No automatic sweep: cook records outlive their windows on a suspended
    # kitchen, and only the head chef knows they're done (`kitchen sweep`).
    if not has_session(name):
        tmux("new-session", "-d", "-s", SESSION, "-n", "_placeholder", kitchen=name)
    mcp_config = {"mcpServers": channel_servers(name)}
    (base / MCP_CONFIG_NAME).write_text(json.dumps(mcp_config, indent=2) + "\n")
    # Self-heal kitchens opened before the rename: a legacy base/.mcp.json is
    # cook-discoverable and would still auto-spawn a channel-server.
    (base / LEGACY_MCP_CONFIG_NAME).unlink(missing_ok=True)

    if resuming:
        print(f"Kitchen \"{name}\" — sous chef back on the line.")
    else:
        print(f"Kitchen \"{name}\" is open. Sous chef on the line.")
    print(f"   {attach_cmd(name)}")

    sous_md = _PKG_DIR / "sous-chef.md"
    if not sous_md.exists():
        sys.exit(f"sous-chef.md not found at {sous_md}")

    spawn_sous(name, base, sous_md.read_text(), project, slug=slug,
               resume_session_id=sous_session_id, model=args.model)


def _seed_codex_home(base: Path, name: str, cwd: str) -> Path:
    """Fresh, isolated CODEX_HOME for a clean-room codex cook. Seeded with ONLY
    auth.json (auth survives — mirrors the Claude "keep auth, drop everything
    else" stance) plus a minimal config.toml granting trust to cwd. The trust
    grant is load-bearing: codex's workspace-trust prompt is suppressed by
    NEITHER --dangerously-bypass-approvals-and-sandbox NOR a -c override, only
    by the persisted projects.<cwd>.trust_level (verified empirically) — without
    it the cook hangs on the trust dialog and wait_for_prompt times out. notify
    rides the command line, so cook→sous reporting is unaffected."""
    home = base / "codex-home" / name
    if home.exists():
        shutil.rmtree(home)  # re-hire under the same name must start fresh
    home.mkdir(parents=True)
    shutil.copyfile(Path.home() / ".codex" / "auth.json", home / "auth.json")
    # Escape cwd for a TOML basic string (backslash + double-quote) so a path
    # with those chars stays valid TOML — otherwise codex can't parse the trust
    # grant and falls back to the (blocking) trust prompt.
    key = cwd.replace("\\", "\\\\").replace('"', '\\"')
    (home / "config.toml").write_text(f'[projects."{key}"]\ntrust_level = "trusted"\n')
    return home


def _validate_skill_path(raw: str) -> str:
    """Resolve a --with-skill path and confirm it's a loadable skill or plugin
    dir (has SKILL.md, or a .claude-plugin/plugin.json). Returns the absolute
    path; exits clearly otherwise. Both shapes load via claude's --plugin-dir
    (verified empirically)."""
    p = Path(raw).expanduser()
    if not p.is_dir():
        sys.exit(f"--with-skill path is not a directory: {raw}")
    if not ((p / "SKILL.md").is_file() or (p / ".claude-plugin" / "plugin.json").is_file()):
        sys.exit(f"--with-skill path is not a skill or plugin dir (needs SKILL.md or .claude-plugin/plugin.json): {raw}")
    return str(p.resolve())


def cmd_hire(args):
    kitchen = resolve_kitchen(args.kitchen)
    base = state_dir(kitchen)
    name = args.name
    backend = args.backend
    cwd = args.project or os.getcwd()
    cwd = str(resolve_project(cwd))

    clean_room = getattr(args, "clean_room", False)
    # Clean-room isolation supports claude + codex. Gemini isolation is a
    # documented follow-up — fail clearly rather than silently no-op.
    if clean_room and backend not in ("claude", "codex"):
        sys.exit(f"--clean-room is only supported for Claude and Codex cooks, not '{backend}' (not yet implemented).")

    # --with-skill: opt a custom skill into a clean-room cook (allowlist v1).
    # Each path loads as a session-scoped --plugin-dir (see build_shell_cmd) —
    # purely additive, clean-room's blank slate is otherwise untouched.
    with_skill = getattr(args, "with_skill", None) or []
    plugin_dirs = None
    if with_skill:
        if not clean_room:
            sys.exit("--with-skill requires --clean-room.")
        if backend != "claude":
            sys.exit(f"--with-skill is not yet supported for '{backend}' cooks (Claude only in v1).")
        plugin_dirs = [_validate_skill_path(p) for p in with_skill]

    # --model: each name belongs to exactly one backend (Claude tiers → claude,
    # astra → codex); a mismatched pairing fails loud rather than silently ignore.
    model = getattr(args, "model", None)
    if model:
        model_backend = "codex" if model in CODEX_MODELS else "claude"
        if backend != model_backend:
            sys.exit(f"--model {model} is only supported for {model_backend} cooks, not '{backend}'.")

    # Clean-room cooks boot bare — NO role prompt. The sous sends the single
    # eval prompt via a ticket. Otherwise resolve the role file as usual.
    if clean_room:
        role_path = None
    else:
        role = getattr(args, "role", None) or "_default"
        role_path = _PKG_DIR / "roles" / f"{role}.md"
        if not role_path.exists():
            valid = sorted(p.stem for p in (_PKG_DIR / "roles").glob("*.md"))
            sys.exit(f"Unknown role '{role}'. Valid roles: {', '.join(valid)}")

    # Gemini-only fail-fast: agy must be on PATH before we write a booting
    # status or spawn the tmux window. Gated to backend=="gemini" so claude,
    # codex, `kitchen open`, and every other command NEVER reference agy — agy
    # is fully optional; the kitchen works without it. The install command is
    # given directly (the POC has no `kitchen setup` agy automation).
    if backend == "gemini" and shutil.which("agy") is None:
        sys.exit(
            "agy not on PATH — install Antigravity CLI: "
            "curl -fsSL https://antigravity.google/cli/install.sh | bash"
        )

    # Claude (file flag) and Gemini (inlined via agy -i) take the role at
    # launch; Codex gets it as a first message via send_keys after the prompt
    # appears. build_shell_cmd reads role_path for the gemini branch.
    role_to_pass = role_path if backend in ("claude", "gemini") else None

    # Clean-room codex cooks get a fresh, isolated CODEX_HOME (no memory,
    # AGENTS.md, plugin registry, or user config). Claude/gemini: None.
    codex_home = str(_seed_codex_home(base, name, cwd)) if clean_room and backend == "codex" else None

    write_status(base, name, {"status": "booting", "agent": name, "backend": backend})

    effort = getattr(args, "effort", None)
    ok = spawn_window(
        kitchen=kitchen, name=name, cwd=cwd,
        backend=backend, status_dir=str(base),
        effort=effort, role_path=role_to_pass, clean_room=clean_room,
        codex_home=codex_home, plugin_dirs=plugin_dirs, model=model,
    )
    if not ok:
        # update_status preserves durable fields (the booting write above
        # set `backend`); benign at hire-time but enforces the §Status
        # preservation invariant by code rather than happenstance.
        update_status(base, name, status="failed")
        # Don't leave a copied credential behind on a failed launch.
        if codex_home:
            shutil.rmtree(codex_home, ignore_errors=True)
        print(f"Failed to boot {name}.", file=sys.stderr)
        sys.exit(1)

    # Keep this barrier: the sous will call `kitchen ticket <cook>` immediately
    # after hire returns. Without it, the first ticket lands in a booting pane
    # and is lost. Also required for the codex send_keys role delivery below.
    try:
        failure = None if wait_for_prompt(kitchen, name, backend) else "didn't show prompt within timeout"
    except PromptBlocked as e:
        failure = str(e)
    if failure:
        update_status(base, name, status="failed")
        if codex_home:
            shutil.rmtree(codex_home, ignore_errors=True)
        sys.exit(f"{name}: {failure}.")

    # Name the codex thread `[<kitchen>/<cook>]` so it is identifiable in the
    # ChatGPT app and `codex resume`. /rename before the first turn creates the
    # thread under that name, and Codex's generated title then leaves it alone.
    # Clean-room threads live in a throwaway CODEX_HOME, so they stay bare.
    if backend == "codex" and not clean_room:
        send_keys(kitchen, name, f"/rename [{kitchen}/{name}]", backend=backend)

    # Clean-room codex cooks have no role_path — they boot bare and the sous
    # tickets the eval prompt (same as clean-room claude).
    if backend == "codex" and role_path:
        send_keys(kitchen, name, role_path.read_text() + _ROLE_ACK_FOOTER,
                  backend=backend)

    print(f"{name} is on the station. Yes, chef!")


def _ticket_sous(kitchen: str, message: str):
    """Hand another sous a ticket over its kitchen's channel socket.

    The socket, not tmux: a sous runs in the head chef's terminal, so it
    has no window to paste into. `cook` carries the sender's kitchen, which is
    where the receiver sends its answer. No status write: a sous has no cook
    record."""
    from claude_kitchen.channel import deliver_to_socket, SOCK_NAME
    sender = os.environ.get("AGENT_KITCHEN")
    if not sender:
        sys.exit("`kitchen ticket sous` must run from a sous: AGENT_KITCHEN is "
                 "unset, so the receiver would have no kitchen to answer.")
    sock = state_dir(kitchen) / SOCK_NAME
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        deliver_to_socket(sock, {"cook": sender, "summary": message, "ts": ts})
    except OSError as e:
        sys.exit(f"Ticket NOT delivered: the sous of kitchen \"{kitchen}\" is not "
                 f"listening on {sock} ({e.strerror or e}).")
    print(f"Ticket fired to the sous of {kitchen}. Heard!")


def cmd_ticket(args):
    kitchen = resolve_kitchen(args.kitchen)
    base = state_dir(kitchen)
    name = args.cook
    message = args.message

    if name == "sous":
        _ticket_sous(kitchen, message)
        return

    backend = (read_status(base, name) or {}).get("backend")
    send_keys(kitchen, name, message, backend=backend)

    # Claude's UserPromptSubmit hook owns its start state; a write here could
    # land after a fast turn's Stop and leave it "working". Codex and Gemini
    # only report completion, so this is their start signal.
    if backend != "claude":
        update_status(base, name, status="working")
    print(f"Ticket fired to {name}. Heard!")


def cmd_peek(args):
    kitchen = resolve_kitchen(args.kitchen)
    base = state_dir(kitchen)
    content = capture_pane(kitchen, args.cook, full=args.full)
    if content:
        print(content)
    else:
        print("(screen is empty)")
    # Surface live busy/idle from the pane tail. backend comes from the cook's
    # state file (pane_busy needs it — markers are backend-specific).
    backend = (read_status(base, args.cook) or {}).get("backend")
    if backend:
        state = "busy" if pane_busy(kitchen, args.cook, backend) else "idle"
        print(f"\n[{args.cook}: {state}]")


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
    kitchens = list_kitchens()
    if kitchen:
        kitchens = [k for k in kitchens if k == kitchen]

    if not kitchens:
        print("No active kitchens.")
        return

    for name in kitchens:
        base = state_dir(name)
        windows = list_windows(name)

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


def _archive_codex_thread(base: Path, cook: str):
    """Archive a departing Codex cook's thread so it drops out of the ChatGPT
    app's session list. Must run after the cook's window is gone — codex holds
    its own thread lock while alive. Clean-room cooks are skipped: their thread
    lives in a private CODEX_HOME that is deleted anyway. A failure (typically
    the ChatGPT app holding the thread's lock) is reported, never fatal."""
    status = read_status(base, cook) or {}
    thread_id = status.get("session_id")
    if status.get("backend") != "codex" or not thread_id or (base / "codex-home" / cook).is_dir():
        return
    try:
        result = subprocess.run(["codex", "archive", thread_id], capture_output=True, text=True)
        reason = (result.stderr.strip().splitlines() or ["no output"])[-1] if result.returncode else None
    except OSError as e:
        reason = str(e)
    if reason:
        print(f"{cook}: Codex thread {thread_id} not archived ({reason}); "
              f"run `codex archive {thread_id}` later.", file=sys.stderr)


def cmd_clock_out(args):
    kitchen = resolve_kitchen(args.kitchen)
    tmux("kill-window", "-t", target(args.cook), kitchen=kitchen)
    base = state_dir(kitchen)
    _archive_codex_thread(base, args.cook)
    # Clean up status file
    cook_file = base / "cooks" / f"{args.cook}.json"
    cook_file.unlink(missing_ok=True)
    # Tear down a clean-room codex cook's per-cook CODEX_HOME if it has one.
    codex_home = base / "codex-home" / args.cook
    if codex_home.is_dir():
        shutil.rmtree(codex_home)
    print(f"{args.cook} has clocked out.")


def _hook_gate() -> tuple[str, str, Path] | None:
    """Check env vars required for hooks. Returns (name, kitchen, status_dir) or None."""
    name = os.environ.get("AGENT_NAME", "")
    kitchen = os.environ.get("AGENT_KITCHEN", "")
    status = os.environ.get("STATUS_DIR", "")
    if not (name and kitchen and status):
        return None
    return name, kitchen, Path(status)


def cmd_hook(args):
    """Handle hook events from Claude Code (stdin) or Codex (--message arg)."""
    ctx = _hook_gate()
    if not ctx:
        return
    name, kitchen, base = ctx

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
        # Claude branch dispatches by event type. UserPromptSubmit is the one
        # "cook started working" write for Claude. Must NOT read
        # last_assistant_message (not present on this event) and must NOT
        # call send_to_socket (not a completion).
        # update_status (not write_status) preserves durable fields like
        # `tokens` and `backend` across this non-completion transition.
        if event == "UserPromptSubmit":
            update_status(
                base, name,
                agent=name, kitchen=kitchen, status="working",
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
        "agent": name, "kitchen": kitchen, "status": "idle",
        "ts": ts, "summary": summary,
    }
    if codex:
        thread_id = payload.get("thread-id", "")
        # Codex runs ephemeral side threads (e.g. title generation) that fire
        # notify too. The cook's thread is pinned on its first turn — the one
        # whose thread has a rollout (side threads never write one) — and
        # after that only the pinned thread counts, however old it gets.
        pinned = (read_status(base, name) or {}).get("session_id")
        rollout = _codex_rollout(thread_id)
        if thread_id != pinned and (pinned or rollout is None):
            return
        updates["session_id"] = thread_id
        # Codex doesn't carry usage in the notify payload — read it from
        # the cook's rollout JSONL.
        tokens = _read_codex_token_count(rollout) if rollout else None
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


def _agy_summary(payload: dict) -> str:
    """Last non-empty PLANNER_RESPONSE.content from agy's transcript JSONL
    (payload['transcriptPath']), or "" on any degraded case (missing path,
    nonexistent/unreadable file, no usable line). Best-effort, never raises —
    the channel still surfaces "cook finished" even when the message is
    unrecoverable. Empty PLANNER_RESPONSE entries are placeholders interleaved
    with tool-call events, so they're filtered out (POC: a simple try/except;
    the full error table in the v2 spec is out of scope)."""
    tp = payload.get("transcriptPath")
    if not tp or not Path(tp).exists():
        return ""
    summary = ""
    try:
        with Path(tp).open() as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # `content` must be a non-empty str: under schema drift a
                # PLANNER_RESPONSE could carry a list/obj, and `.rstrip` on that
                # would AttributeError out of the OSError guard, breaking the
                # "never raises" contract. Skip non-string entries.
                content = obj.get("content")
                if obj.get("type") == "PLANNER_RESPONSE" and isinstance(content, str) and content:
                    summary = content.rstrip("\n")
    except OSError:
        return ""
    return summary


def cmd_hook_agy(args):
    """Antigravity (gemini cook) Stop hook → channel notification. Reads the
    Stop payload from stdin.

    Standalone from cmd_hook — leaves cmd_hook and its sous carveout untouched.
    Reuses _hook_gate for the AGENT_NAME/STATUS_DIR multi-tenancy guard: a bare
    `agy` session the head chef runs has no AGENT_NAME, so the gate returns None
    and this no-ops — kitchen never clobbers ad-hoc agy. gemini has no token
    reader, so the push carries NO ctx (channel.py omits the attribute when the
    key is absent)."""
    gate = _hook_gate()
    if not gate:
        return
    name, kitchen, base = gate
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    summary = _agy_summary(payload)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # update_status (not write_status) preserves durable fields like `backend`
    # (written "gemini" at hire) across this completion transition.
    update_status(base, name, agent=name, kitchen=kitchen, status="idle",
                  ts=ts, summary=summary, session_id=payload.get("conversationId", ""))
    from claude_kitchen.channel import send_to_socket, SOCK_NAME
    send_to_socket(base / SOCK_NAME, {"cook": name, "summary": summary, "ts": ts})


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


def _codex_rollout(thread_id):
    """Find the cook's Codex rollout JSONL under $CODEX_HOME/sessions
    (matched on `thread-id` from the notify payload). Returns its path, or
    None if it isn't found within the 7-day scan-back window.

    Scan order: today, then walk back day-by-day. Codex writes to
    YYYY/MM/DD/ in UTC; sessions can span midnight, hence the walk."""
    if not thread_id:
        return None
    base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
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
                return f
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


def _install_claude_hooks(settings: Path) -> list[str]:
    """Append a `kitchen hook` entry to each Claude hook event that lacks one,
    leaving every other key and hook untouched. Backs the file up and keeps its
    mode before writing. Returns the events it added."""
    cmd = {"type": "command", "command": "kitchen hook"}
    entries = {"Stop": {"hooks": [cmd]}, "UserPromptSubmit": {"matcher": "", "hooks": [cmd]}}
    raw = settings.read_text() if settings.exists() else None
    data = json.loads(raw) if raw else {}
    hooks = data.setdefault("hooks", {})
    added = [ev for ev in entries if not any(
        h.get("command") == cmd["command"] for e in hooks.get(ev, []) for h in e.get("hooks", []))]
    if not added:
        return added
    for ev in added:
        hooks.setdefault(ev, []).append(entries[ev])
    mode = 0o600
    if raw is not None:
        mode = settings.stat().st_mode & 0o777
        backup = settings.with_name(f"{settings.name}.bak-kitchen-{datetime.now():%Y%m%d-%H%M%S}")
        atomic_write_json(backup, json.loads(raw), mode=mode)
    atomic_write_json(settings, data, mode=mode)
    return added


def cmd_setup(args):
    """Install kitchen's Claude hooks, check the environment, print what's needed."""
    all_good = True
    codex_hook_cmd = "kitchen"

    # --- Claude Code hooks ---
    claude_settings = Path.home() / ".claude" / "settings.json"
    added = _install_claude_hooks(claude_settings)
    print(f"✅ Claude hooks {'added (' + ', '.join(added) + ') to ' + str(claude_settings) if added else 'installed'}")

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
    cache = Path.home() / ".claude" / "plugins" / "cache"
    sp_paths = [cache / m / "superpowers" for m in SUPERPOWERS_MARKETPLACES]
    sp_path = next((p for p in sp_paths if p.exists()), None)
    if sp_path:
        print(f"✅ superpowers plugin installed ({sp_path})")
    else:
        all_good = False
        print("❌ superpowers plugin not found")
        print(f"   Expected at: {' or '.join(map(str, sp_paths))}")
        print("   Install via Claude Code: /plugin install superpowers@claude-plugins-official")
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

    # --- stray root-level .mcp.json (auto-remove; §Design.4 landmine) ---
    # A `.mcp.json` at the state root is an ancestor of every cook cwd, so a
    # cook walking up the tree auto-discovers it and spawns a rogue
    # channel-server (cooks launch with --dangerously-skip-permissions, so the
    # MCP approval gate doesn't protect them). Layer 1 stops it being recreated;
    # this removes a stale one left from before the fix. Only the literal
    # state-root `.mcp.json` is touched — per-kitchen `kitchen-mcp.json` configs
    # are left alone.
    root_mcp = Path.home() / ".claude-kitchen" / ".mcp.json"
    if root_mcp.exists():
        root_mcp.unlink()
        print(f"✅ Removed stray root-level MCP config: {root_mcp}")

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


def cmd_suspend(args):
    """Kill a kitchen's tmux server and touch nothing on disk.

    `close` without the destruction: the cooks are gone, the kitchen's memory —
    kitchen.json, cooks/*.json, notes/, the wiki — is not. Winding a kitchen
    down used to mean destroying it, so nobody ever did, which is how one tmux
    server ended up carrying 16 kitchens and 117 panes.

    kill-server rather than kill-session: the point is to give the machine the
    process back, and the kitchen owns its server outright."""
    kitchen = resolve_kitchen(args.kitchen)
    if not has_session(kitchen):
        sys.exit(f"Kitchen \"{kitchen}\" has no running tmux server — already suspended.")
    kill_server(kitchen)
    print(f"Kitchen \"{kitchen}\" suspended. Cooks are gone; notes and cook records are untouched.")
    print(f"   kitchen open --resume {kitchen}    # bring the sous back")
    print(f"   {attach_cmd(kitchen)}    # once it is back up")


def cmd_close(args):
    kitchen = resolve_kitchen(args.kitchen)
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

    kill_server(kitchen)
    for cook_file in sorted((base / "cooks").glob("*.json")):
        _archive_codex_thread(base, cook_file.stem)

    # Clean up mcp config, socket, pid, and stale cook state
    for f in (MCP_CONFIG_NAME, LEGACY_MCP_CONFIG_NAME, "kitchen.sock", "sous.pid"):
        (base / f).unlink(missing_ok=True)
    for sub in ("cooks", "notes", "codex-home"):
        d = base / sub
        if d.is_dir():
            shutil.rmtree(d)

    print("Kitchen closed. Service over.")


def main():
    parser = argparse.ArgumentParser(prog="kitchen", description="claude-kitchen CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="Open a kitchen")
    p_open.add_argument("name", nargs="?", default=None, help="Kitchen/worktree name (omit to use cwd without worktree)")
    p_open.add_argument("project", nargs="?", default=".", help="Project path or name (default: cwd)")
    p_open.add_argument("--worktree-path", help="Custom path for the worktree (default: sibling directory)")
    p_open.add_argument("--resume", action="store_true", help="Resume the previous sous conversation (uses sous_session_id from kitchen.json)")
    p_open.add_argument("--model", choices=CLAUDE_MODELS,
                        help="Claude model tier for the sous: fable, sonnet, or opus. Passed to `claude --model` verbatim, resolving the latest model in that tier. Omit to use the account default.")

    p_hire = sub.add_parser("hire", help="Hire a cook")
    p_hire.add_argument("name", help="Cook name")
    p_hire.add_argument("--backend", default="claude", choices=["claude", "codex", "gemini"])
    p_hire.add_argument("--kitchen", help="Target kitchen")
    p_hire.add_argument("--project", help="Project path (defaults to cwd)")
    p_hire.add_argument("--role", help="Role from src/claude_kitchen/roles/ (all backends)")
    p_hire.add_argument("--effort", help="Reasoning effort (low, medium, high, xhigh, max, ultra; support is backend/model-dependent, Claude maps ultra to max)")
    p_hire.add_argument("--model", choices=[*CLAUDE_MODELS, *CODEX_MODELS], help="Model for the cook: fable, sonnet, or opus (Claude cooks; passed to `claude --model` verbatim, resolving the latest model in that tier), or astra (Codex cooks; passed to `codex -m` as gpt-6-astra). Omit to use the account default.")
    p_hire.add_argument("--clean-room", action="store_true", help="Isolated eval hire (Claude or Codex): no memory, no plugin/skill startup injection, no role prompt. Sous supplies the one eval prompt via a ticket. (gemini not yet supported)")
    p_hire.add_argument("--with-skill", action="append", default=[], metavar="PATH", help="Load a custom skill/plugin dir into a --clean-room cook (repeatable; Claude only). Path needs a SKILL.md or .claude-plugin/plugin.json. Additive opt-in to the blank slate.")

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

    p_suspend = sub.add_parser("suspend", help="Kill a kitchen's tmux server, keeping all its state on disk")
    p_suspend.add_argument("kitchen", nargs="?", help="Kitchen name")

    p_close = sub.add_parser("close", help="Close a kitchen")
    p_close.add_argument("kitchen", nargs="?", help="Kitchen name")
    p_close.add_argument("--force", action="store_true", help="Remove worktree even with unpushed changes")
    p_close.add_argument("--keep-worktree", action="store_true", help="Skip worktree removal entirely")

    sub.add_parser("hook", help="Claude Code hook handler (called by hooks, not directly)")

    p_hook_codex = sub.add_parser("hook-codex", help="Codex hook handler (called by notify, not directly)")
    p_hook_codex.add_argument("json_payload", nargs="?", default="{}", help="JSON from Codex")

    sub.add_parser("hook-agy", help="Antigravity/gemini hook handler (called by agy hooks via stdin, not directly)")

    sub.add_parser("setup", help="Install kitchen's Claude hooks and check the environment")

    sub.add_parser(
        "statusline-segment",
        help="Print the kitchen-state segment for embedding in a statusline",
    )

    sub.add_parser("roles", help="List available cook roles")

    p_channel = sub.add_parser("channel-server", help="(internal) Run channel MCP server")
    p_channel.add_argument("kitchen", help="Kitchen name")
    p_plow = sub.add_parser("plow-chat-server", help="(internal) Run the plow-chat channel MCP server")
    p_plow.add_argument("kitchen", help="Kitchen name")

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
    elif args.command == "suspend":
        cmd_suspend(args)
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
    elif args.command == "hook-agy":
        cmd_hook_agy(args)
    elif args.command == "channel-server":
        from claude_kitchen.channel import main as channel_main
        channel_main(args.kitchen)
    elif args.command == "plow-chat-server":
        from claude_kitchen.plow_chat import main as plow_chat_main
        plow_chat_main(args.kitchen)
