"""Spawn logic for claude-kitchen agents."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from claude_kitchen.tmux import tmux, has_session, target, SESSION
from claude_kitchen.state import MCP_CONFIG_NAME


def check_sous_pid(state_dir: Path):
    """Exit if another sous is already running for this kitchen.

    Called at the TOP of cmd_open, not from spawn_sous: a refused open must
    change nothing, and everything cmd_open does before launching the sous —
    rewriting kitchen.json and the MCP config, creating the session, and on the
    has_session branch _sweep_cooks — is a mutation."""
    pid_file = state_dir / "sous.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # check if process exists
            sys.exit(f"Sous chef already running (pid {pid}). Exit it first, or kitchen close.")
        except (ProcessLookupError, ValueError):
            pass  # stale pid, fine to proceed


def spawn_sous(kitchen: str, state_dir: Path, sous_prompt: str,
               project: Path = None, slug: str = None,
               resume_session_id: str = None):
    """Replace current process with Claude as sous chef."""
    # Write our PID before exec — exec preserves the PID
    (state_dir / "sous.pid").write_text(str(os.getpid()))

    # AGENT_KITCHEN, not AGENT_SESSION: the tmux session is a constant now, so
    # the kitchen name is the only thing worth passing down and every consumer
    # (resolve_kitchen, the statusline, the hook gate) reads this.
    os.environ["AGENT_KITCHEN"] = kitchen
    os.environ["AGENT_NAME"] = "sous"
    os.environ["STATUS_DIR"] = str(state_dir)
    if slug:
        from claude_kitchen.state import wiki_dir, notes_dir
        os.environ["KITCHEN_WIKI"] = str(wiki_dir(slug))
        os.environ["KITCHEN_NOTES"] = str(notes_dir(kitchen))

    # --remote-control=<name> names the RC session exactly the kitchen name,
    # so the sous is findable in the mobile session list. Claude cooks get a
    # named `<kitchen>/<cook>` RC session in build_shell_cmd (the CLI remote-
    # controls all sessions by default anyway — naming is the value-add).
    # The `=` form is load-bearing: the flag's arg is optional, so a separate
    # token starting with `-` would parse as the next flag, not the name.
    claude_args = [
        "claude",
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels", "server:kitchen",
        "--mcp-config", str(state_dir / MCP_CONFIG_NAME),
        f"--remote-control={kitchen}",
    ]
    if resume_session_id:
        claude_args.extend(["--resume", resume_session_id])
    claude_args.extend(["--append-system-prompt", sous_prompt])
    if project:
        os.chdir(project)
    os.execvp("claude", claude_args)


def spawn_codex_sous(kitchen: str, state_dir: Path, port: int, thread_id: str,
                     project: Path = None, slug: str = None):
    """Replace current process with Codex as sous chef.

    The claude analogue (spawn_sous) hands Claude its prompt and its channel
    MCP server on the command line. Neither has a codex equivalent, so both
    already happened before this call: cmd_open created the thread with the
    prompt as developerInstructions, and the codex-channel bridge owns the
    socket. All that is left is attaching a TUI to that thread — `resume`
    rejoins it in place, since the app-server already has it loaded."""
    check_sous_pid(state_dir)
    (state_dir / "sous.pid").write_text(str(os.getpid()))

    os.environ["AGENT_KITCHEN"] = kitchen
    os.environ["AGENT_NAME"] = "sous"
    os.environ["STATUS_DIR"] = str(state_dir)
    if slug:
        from claude_kitchen.state import wiki_dir, notes_dir
        os.environ["KITCHEN_WIKI"] = str(wiki_dir(slug))
        os.environ["KITCHEN_NOTES"] = str(notes_dir(kitchen))

    if project:
        os.chdir(project)
    # No --dangerously-bypass-approvals-and-sandbox: approvals and sandbox are
    # properties of the THREAD here, set once at thread/start. A codex cook
    # passes the flag because it owns its own session.
    os.execvp("codex", ["codex", "--remote", f"ws://127.0.0.1:{port}",
                        "resume", thread_id])


# Kitchen passes --effort through to each backend's native scale, aliasing
# only what that backend doesn't take literally. Verified 2026-07-17:
# Claude 2.1.214: low | medium | high | xhigh | max
# Codex 0.144.5 + gpt-5.6-sol/terra: low | medium | high | xhigh | max | ultra
# Codex effort support is model-dependent, so unsupported levels fail there.
_CLAUDE_EFFORT_ALIAS = {"ultra": "max"}

# Per-launch notify override appended to every codex cook. TOML array literal.
_CODEX_NOTIFY_OVERRIDE = 'notify=["kitchen","hook-codex"]'

# Appended to the inlined role for a gemini cook. agy's `-i` submits its
# argument as the first USER turn (not a system prompt), so without this agy
# acts on the role immediately — exploring/running tools instead of idling.
# This clamps it to "acknowledge and wait", the gemini analogue of cli's
# _ROLE_ACK_FOOTER for codex (which has the same role-as-first-message shape).
_GEMINI_ROLE_FOOTER = (
    "\n\n---\n"
    "The text above is your standing ROLE, not a task. Do NOT run any tools, "
    "commands, or investigation now. Reply with one short line (\"Ready, chef.\") "
    "and then wait silently — your actual ticket arrives as the next message.\n"
)


# Clean-room (eval) cooks disable the superpowers plugin so its SessionStart
# "You have superpowers" injection never fires. Passed via --settings, which
# MERGES with ~/.claude/settings.json (empirically verified) — the kitchen's
# own Stop hook there stays live, so cook→sous completion notifications still
# work. Plugin key is the marketplace-qualified name confirmed against
# ~/.claude/settings.json's enabledPlugins.
_CLEAN_ROOM_SETTINGS = '{"enabledPlugins":{"superpowers@superpowers-marketplace":false}}'


def build_shell_cmd(backend: str, name: str, kitchen: str, status_dir: str,
                    effort: str = None, role_path: Path = None,
                    clean_room: bool = False, codex_home: str = None,
                    plugin_dirs: list = None, model: str = None) -> str:
    q = shlex.quote
    parts = [
        f"AGENT_NAME={q(name)}",
        f"AGENT_KITCHEN={q(kitchen)}",
        f"STATUS_DIR={q(status_dir)}",
    ]
    parts.extend(f"{k}={q(v)}" for k, v in os.environ.items() if k.startswith("KITCHEN_"))
    # Clean-room codex cooks run against a fresh, isolated CODEX_HOME (seeded in
    # cmd_hire) — no user config, memory, AGENTS.md, or plugin registry. notify
    # still rides the -c flag below, so cook→sous reporting is unaffected. Gated
    # to codex so the invariant (only codex gets CODEX_HOME) is local here.
    if codex_home and backend == "codex":
        parts.append(f"CODEX_HOME={q(codex_home)}")
    env = "export " + " ".join(parts)
    if backend == "claude":
        effort = _CLAUDE_EFFORT_ALIAS.get(effort, effort)
        effort_flag = f" --effort {q(effort)}" if effort else ""
        # Claude model selection (--model): the tier alias (fable/sonnet/opus)
        # passes through verbatim, so `claude --model` resolves the latest model
        # in that tier at launch. Omitted → empty string, so the default launch
        # command is byte-for-byte unchanged.
        model_flag = f" --model {q(model)}" if model else ""
        # Pass the file path, not the contents — the file form avoids
        # shell-quoting fragility for multi-line role prompts.
        role_flag = f" --append-system-prompt-file {q(str(role_path))}" if role_path else ""
        # Disable Claude's prompt-suggestion ghost text (the dim placeholder in
        # an empty composer): pane readers (pane_busy/peek) could otherwise
        # misread it as typed input. Claude cooks only — appended to THIS
        # branch's export, not the shared `parts` list, so it never spills onto
        # codex/gemini cooks.
        env = f"{env} CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false"
        # Clean-room (eval) cooks: disable auto-memory via env var (no MEMORY.md
        # <system-reminder> injection) and disable the superpowers plugin via
        # --settings (no SessionStart injection). Both ride the launch without
        # touching subscription auth or the kitchen Stop hook. The caller also
        # passes no role_path, so clean-room cooks boot bare (no role prompt).
        mem = "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 " if clean_room else ""
        settings_flag = f" --settings {q(_CLEAN_ROOM_SETTINGS)}" if clean_room else ""
        # Clean-room allowlist opt-in (--with-skill): each path is loaded as a
        # session-scoped plugin dir. Additive only — doesn't touch memory, the
        # disabled superpowers plugin, the role, or the sous-managed cwd. cli.py
        # gates this to clean-room claude cooks with validated paths.
        plugin_flags = "".join(f" --plugin-dir {q(p)}" for p in (plugin_dirs or []))
        # Block AskUserQuestion: it renders an interactive picker in the cook's
        # TUI that fires no hook and blocks forever — the sous never learns of
        # it. Cooks surface questions via NEEDS_CONTEXT instead (see role prompts).
        # Name the cook's Remote Control session `<kitchen>/<cook>` so it's
        # identifiable on mobile (RC is CLI-default-on; this only names it).
        # `=` form: the flag's arg is optional, so a leading-dash name as a
        # separate token would parse as the next flag instead of the name.
        # `<kitchen>/<cook>`. This used to strip the ck- prefix off the session
        # name; the session is a constant now, so it comes straight from the
        # kitchen. Getting this wrong would silently rename every cook on the
        # head chef's phone to "kitchen/<cook>".
        rc_name = f"{kitchen}/{name}"
        rc_flag = f" --remote-control={q(rc_name)}"
        return f'bash -lc {q(f"{env}; {mem}exec claude --dangerously-skip-permissions --disallowedTools AskUserQuestion{rc_flag}{effort_flag}{model_flag}{settings_flag}{plugin_flags}{role_flag}")}'
    elif backend == "codex":
        # Codex has no --append-system-prompt-file equivalent. Role delivery
        # happens via send_keys after wait_for_prompt (see cmd_hire).
        effort_flag = f' -c model_reasoning_effort={q(effort)}' if effort else ""
        # Per-launch notify override. Bypasses any global notify wrapper
        # (e.g. the Codex Computer Use plugin's SkyComputerUseClient, which
        # rewrites ~/.codex/config.toml's top-level notify to wrap kitchen
        # in --previous-notify and then silently swallows the forward inside
        # workspaces enrolled with Codex Desktop). Codex CLI reads notify
        # from config at process start; -c overrides per-process without
        # touching global state. Value is a TOML array literal.
        notify_flag = f' -c {q(_CODEX_NOTIFY_OVERRIDE)}'
        return f'bash -lc {q(f"{env}; exec codex --dangerously-bypass-approvals-and-sandbox{effort_flag}{notify_flag}")}'
    elif backend == "gemini":
        # agy (Antigravity CLI) drives Gemini. No --append-system-prompt-file
        # equivalent, so the role is INLINED as the first interactive turn via
        # -i (shlex-quoted; a ~2.4KB role is well under the argv limit). The
        # `< /dev/null` redirect is load-bearing: agy reads stdin even with the
        # prompt on argv and hangs on launch without it. agy honors no --effort
        # flag, so effort is silently dropped (POC).
        role_flag = f" -i {q(role_path.read_text() + _GEMINI_ROLE_FOOTER)}" if role_path else ""
        return f'bash -lc {q(f"{env}; exec agy{role_flag} --dangerously-skip-permissions < /dev/null")}'
    else:
        raise ValueError(f"Unknown backend: {backend}")


def spawn_window(kitchen: str, name: str, cwd: str, backend: str, status_dir: str,
                 effort: str = None, role_path: Path = None,
                 clean_room: bool = False, codex_home: str = None,
                 plugin_dirs: list = None, model: str = None) -> bool:
    """Spawn a new tmux window with an agent. Returns True on success."""
    cmd = build_shell_cmd(backend, name, kitchen, status_dir,
                          effort=effort, role_path=role_path,
                          clean_room=clean_room, codex_home=codex_home,
                          plugin_dirs=plugin_dirs, model=model)

    if has_session(kitchen):
        result = tmux("new-window", "-t", target(), "-n", name, "-c", cwd, cmd,
                      kitchen=kitchen)
    else:
        result = tmux("new-session", "-d", "-s", SESSION, "-n", name, "-c", cwd, cmd,
                      kitchen=kitchen)

    return result.returncode == 0


def build_sous_cmd(name: str, base: Path, sous_md_path: Path,
                   slug: str = None, parent_base: Path = None) -> str:
    """Build the `bash -lc` command that launches a child sous in a tmux
    window — the windowed analogue of spawn_sous's in-place execvp argv.

    Three deliberate differences from a root sous (spawn_sous): NO
    --remote-control (POC scope), --disallowedTools AskUserQuestion (nobody is
    watching this window), and the sous prompt is delivered via
    --append-system-prompt-file (the cook role-file pattern) rather than
    --append-system-prompt, keeping the multi-line prompt out of the
    shell-quoted command string.

    STATUS_DIR stays THIS kitchen's own base so the child's own cooks +
    resume-session capture keep working. parent_base, when set, is exported
    separately as PARENT_STATUS_DIR so the child sous's Stop hook reports UP
    to the parent kitchen's channel socket (see the cmd_hook sous carveout).
    """
    q = shlex.quote
    parts = [
        "AGENT_NAME=sous",
        f"AGENT_KITCHEN={q(name)}",
        f"STATUS_DIR={q(str(base))}",
    ]
    if slug:
        from claude_kitchen.state import wiki_dir, notes_dir
        parts.append(f"KITCHEN_WIKI={q(str(wiki_dir(slug)))}")
        parts.append(f"KITCHEN_NOTES={q(str(notes_dir(name)))}")
    if parent_base is not None:
        parts.append(f"PARENT_STATUS_DIR={q(str(parent_base))}")
    env = "export " + " ".join(parts)
    claude = (
        # Block AskUserQuestion, same as a cook (build_shell_cmd): it renders an
        # interactive picker in a tmux window nobody is watching and blocks
        # forever. A sub-sous has a working escalation route — it reports UP
        # through PARENT_STATUS_DIR — so given both paths it must take that one.
        # The ROOT sous keeps the tool: the head chef sits in front of it.
        "exec claude --dangerously-skip-permissions "
        "--disallowedTools AskUserQuestion "
        "--dangerously-load-development-channels server:kitchen "
        f"--mcp-config {q(str(base / MCP_CONFIG_NAME))} "
        f"--append-system-prompt-file {q(str(sous_md_path))}"
    )
    return f'bash -lc {q(f"{env}; {claude}")}'


def spawn_sous_window(name: str, base: Path, sous_md_path: Path, project: Path,
                      slug: str = None, parent_base: Path = None) -> bool:
    """Launch a child sous in window `sous` of the kitchen's own tmux session,
    then drop the `_placeholder` window cmd_open created. The whole child
    kitchen (its sous + its future cooks) lives in this one session.

    Used by `kitchen open --sub-sous` instead of spawn_sous's in-place
    os.execvp, so the caller (a parent sous's Bash tool subprocess) keeps its
    own process. Returns True if the sous window spawned; False lets cmd_open
    tear the half-created kitchen down."""
    cmd = build_sous_cmd(name, base, sous_md_path, slug=slug, parent_base=parent_base)
    if tmux("new-window", "-t", target(), "-n", "sous", "-c", str(project),
            cmd, kitchen=name).returncode != 0:
        return False
    # The placeholder kill is cosmetic (the window is `_`-hidden from brigade);
    # a transient stall under launch load must not fail an otherwise-good launch.
    try:
        tmux("kill-window", "-t", target("_placeholder"), kitchen=name)
    except subprocess.TimeoutExpired:
        pass
    # Record the sous pane's PID (parity with the execvp sous's sous.pid). It's
    # the pane's root process — a liveness handle that lets a later non-sub-sous
    # `kitchen open` of this kitchen detect the running sous (dup protection).
    # Best-effort: the window already launched, so a TimeoutExpired here must NOT
    # propagate (cmd_open would read it as a launch failure and tear the window
    # down) — just skip the pid.
    try:
        panes = tmux("list-panes", "-t", target("sous"), "-F", "#{pane_pid}",
                     kitchen=name)
        if panes.returncode == 0 and panes.stdout.strip():
            (base / "sous.pid").write_text(panes.stdout.strip().splitlines()[0] + "\n")
    except subprocess.TimeoutExpired:
        pass
    return True
