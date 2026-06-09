"""Spawn logic for claude-kitchen agents."""
import os
import shlex
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
    # Surface the overview dashboard URL in every sous's env so the statusline
    # segment can show it. Port matches the dashboard server convention.
    _port = os.environ.get("KITCHEN_DASHBOARD_PORT", "5757")
    os.environ["KITCHEN_DASHBOARD_URL"] = f"http://127.0.0.1:{_port}"
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


def spawn_overview_loop(base: Path, port: str = "5757"):
    """Launch the overview kitchen in a DETACHED `ck-overview` tmux session with
    two windows that live and die together:

      - `server`: the FastAPI dashboard (`kitchen dashboard-server`)
      - `loop`  : the Python summarizer loop (`kitchen overview-loop`)

    There is no resident Claude — the `loop` window execs the Python loop
    directly, which spawns a fresh `claude -p` one-shot per changed kitchen.
    Both windows inherit the dashboard URL/port via exported env.
    """
    q = shlex.quote
    session = mc("overview")
    dashboard_url = f"http://127.0.0.1:{port}"
    env = "export " + " ".join([
        f"AGENT_SESSION={session}",
        f"STATUS_DIR={q(str(base))}",
        f"KITCHEN_DASHBOARD_URL={q(dashboard_url)}",
        f"KITCHEN_DASHBOARD_PORT={q(str(port))}",
        f"KITCHEN_NOTES={q(str(base / 'notes'))}",
        f"KITCHEN_WIKI={q(str(base / 'wiki'))}",
    ])
    server_inner = f"{env}; exec kitchen dashboard-server"
    loop_inner = f"{env}; exec kitchen overview-loop"
    server_cmd = f"bash -lc {q(server_inner)}"
    loop_cmd = f"bash -lc {q(loop_inner)}"
    tmux("new-session", "-d", "-s", session, "-n", "server", "-c", str(base), server_cmd)
    tmux("new-window", "-t", session, "-n", "loop", "-c", str(base), loop_cmd)
