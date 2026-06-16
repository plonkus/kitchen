"""Spawn logic for claude-kitchen agents."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from claude_kitchen.tmux import tmux, has_session, mc


def _check_sous_pid(state_dir: Path):
    """Exit if another sous is already running for this kitchen."""
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
    _check_sous_pid(state_dir)

    # Write our PID before exec — exec preserves the PID
    (state_dir / "sous.pid").write_text(str(os.getpid()))

    session = mc(kitchen)
    os.environ["AGENT_SESSION"] = session
    os.environ["AGENT_NAME"] = "sous"
    os.environ["STATUS_DIR"] = str(state_dir)
    if slug:
        from claude_kitchen.state import wiki_dir, notes_dir
        os.environ["KITCHEN_WIKI"] = str(wiki_dir(slug))
        os.environ["KITCHEN_NOTES"] = str(notes_dir(kitchen))

    # --remote-control is sous-only (unconditional for v1, no opt-out).
    # The prefix makes auto-generated RC session names identifiable per
    # kitchen — e.g. `dashboard-abc123` instead of host-default `mbp-abc123`.
    # Cooks (claude OR codex) do NOT get remote control: build_shell_cmd
    # is untouched.
    claude_args = [
        "claude",
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels", "server:kitchen",
        "--mcp-config", str(state_dir / ".mcp.json"),
        "--remote-control",
        "--remote-control-session-name-prefix", kitchen,
    ]
    if resume_session_id:
        claude_args.extend(["--resume", resume_session_id])
    claude_args.extend(["--append-system-prompt", sous_prompt])
    if project:
        os.chdir(project)
    os.execvp("claude", claude_args)


# Kitchen uses a unified effort scale. Map to each backend's native values.
# Kitchen:  low | medium | high | max
# Claude:   low | medium | high | max
# Codex:    low | medium | high | xhigh
_CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "xhigh"}

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


def build_shell_cmd(backend: str, name: str, session: str, status_dir: str,
                    effort: str = None, role_path: Path = None) -> str:
    q = shlex.quote
    parts = [
        f"AGENT_NAME={q(name)}",
        f"AGENT_SESSION={q(session)}",
        f"STATUS_DIR={q(status_dir)}",
    ]
    parts.extend(f"{k}={q(v)}" for k, v in os.environ.items() if k.startswith("KITCHEN_"))
    env = "export " + " ".join(parts)
    if backend == "claude":
        effort_flag = f" --effort {q(effort)}" if effort else ""
        # Pass the file path, not the contents — the file form avoids
        # shell-quoting fragility for multi-line role prompts.
        role_flag = f" --append-system-prompt-file {q(str(role_path))}" if role_path else ""
        # Block AskUserQuestion: it renders an interactive picker in the cook's
        # TUI that fires no hook and blocks forever — the sous never learns of
        # it. Cooks surface questions via NEEDS_CONTEXT instead (see role prompts).
        return f'bash -lc {q(f"{env}; exec claude --dangerously-skip-permissions --disallowedTools AskUserQuestion{effort_flag}{role_flag}")}'
    elif backend == "codex":
        # Codex has no --append-system-prompt-file equivalent. Role delivery
        # happens via send_keys after wait_for_prompt (see cmd_hire).
        codex_effort = _CODEX_EFFORT.get(effort, effort) if effort else None
        effort_flag = f' -c model_reasoning_effort={q(codex_effort)}' if codex_effort else ""
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


def spawn_window(session: str, name: str, cwd: str, backend: str, status_dir: str,
                 effort: str = None, role_path: Path = None) -> bool:
    """Spawn a new tmux window with an agent. Returns True on success."""
    cmd = build_shell_cmd(backend, name, session, status_dir,
                          effort=effort, role_path=role_path)

    if has_session(session):
        result = tmux("new-window", "-t", session, "-n", name, "-c", cwd, cmd)
    else:
        result = tmux("new-session", "-d", "-s", session, "-n", name, "-c", cwd, cmd)

    return result.returncode == 0


def build_sous_cmd(name: str, base: Path, sous_md_path: Path,
                   slug: str = None, parent_base: Path = None) -> str:
    """Build the `bash -lc` command that launches a child sous in a tmux
    window — the windowed analogue of spawn_sous's in-place execvp argv.

    Two deliberate differences from a root sous (spawn_sous): NO
    --remote-control (POC scope), and the sous prompt is delivered via
    --append-system-prompt-file (the cook role-file pattern) rather than
    --append-system-prompt, keeping the multi-line prompt out of the
    shell-quoted command string.

    STATUS_DIR stays THIS kitchen's own base so the child's own cooks +
    resume-session capture keep working. parent_base, when set, is exported
    separately as PARENT_STATUS_DIR so the child sous's Stop hook reports UP
    to the parent kitchen's channel socket (see the cmd_hook sous carveout).
    """
    q = shlex.quote
    session = mc(name)
    parts = [
        "AGENT_NAME=sous",
        f"AGENT_SESSION={q(session)}",
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
        "exec claude --dangerously-skip-permissions "
        "--dangerously-load-development-channels server:kitchen "
        f"--mcp-config {q(str(base / '.mcp.json'))} "
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
    session = mc(name)
    cmd = build_sous_cmd(name, base, sous_md_path, slug=slug, parent_base=parent_base)
    if tmux("new-window", "-t", session, "-n", "sous", "-c", str(project),
            cmd).returncode != 0:
        return False
    # The placeholder kill is cosmetic (the window is `_`-hidden from brigade);
    # a transient stall under launch load must not fail an otherwise-good launch.
    try:
        tmux("kill-window", "-t", f"{session}:_placeholder")
    except subprocess.TimeoutExpired:
        pass
    # Record the sous pane's PID (parity with the execvp sous's sous.pid). It's
    # the pane's root process — a liveness handle that lets a later non-sub-sous
    # `kitchen open` of this kitchen detect the running sous (dup protection).
    # Best-effort: the window already launched, so a TimeoutExpired here must NOT
    # propagate (cmd_open would read it as a launch failure and tear the window
    # down) — just skip the pid.
    try:
        panes = tmux("list-panes", "-t", f"{session}:sous", "-F", "#{pane_pid}")
        if panes.returncode == 0 and panes.stdout.strip():
            (base / "sous.pid").write_text(panes.stdout.strip().splitlines()[0] + "\n")
    except subprocess.TimeoutExpired:
        pass
    return True
