"""tmux helpers for claude-kitchen."""
import itertools
import os
import subprocess
import sys
import time
from typing import Optional

TIMEOUT = 5
CK_PREFIX = "ck-"

_buffer_counter = itertools.count()


def tmux(*args: str, timeout: int = TIMEOUT,
         input: Optional[str] = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout,
        input=input, check=check,
    )


def mc(session: str) -> str:
    """Ensure session name has ck- prefix."""
    return session if session.startswith(CK_PREFIX) else CK_PREFIX + session


def bare(session: str) -> str:
    """Strip ck- prefix."""
    return session[len(CK_PREFIX):] if session.startswith(CK_PREFIX) else session


def list_sessions() -> list[str]:
    result = tmux("list-sessions", "-F", "#{session_name}")
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().split("\n")
            if s.strip().startswith(CK_PREFIX)]


def list_windows(session: str) -> list[str]:
    result = tmux("list-windows", "-t", session, "-F", "#{window_name}", check=True)
    return [
        w.strip() for w in result.stdout.strip().split("\n")
        if w.strip() and not w.strip().startswith("_")
    ]


def has_session(session: str) -> bool:
    return tmux("has-session", "-t", session).returncode == 0


def capture_pane(session: str, window: str, full: bool = False) -> Optional[str]:
    cmd = ["capture-pane", "-t", f"{session}:{window}", "-p"]
    if full:
        cmd.extend(["-S", "-"])
    result = tmux(*cmd)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout


# Welcome-box titles appear only after the TUI clears any trust/update/auth
# modal — so their presence is a reliable "chat input is live" signal.
_PROMPT_MARKERS = {
    "claude": "Claude Code v",
    "codex": "OpenAI Codex (v",
}


def wait_for_prompt(session: str, window: str, backend: str, timeout: int = 60) -> bool:
    marker = _PROMPT_MARKERS[backend]
    deadline = time.time() + timeout
    # Codex shows an "Update available!" picker before its welcome banner
    # when a new version is published. Dismiss it once with `3` (= "Skip
    # until next version") + Enter so the welcome marker can appear.
    update_dismissed = False
    while time.time() < deadline:
        content = capture_pane(session, window)
        if content and marker in content:
            return True
        if (backend == "codex" and not update_dismissed and content
                and ("Update available!" in content
                     or "Press enter to continue" in content)):
            tmux("send-keys", "-t", f"{session}:{window}", "3", "Enter",
                 check=True)
            update_dismissed = True
        time.sleep(1)
    return False


def send_keys(session: str, window: str, text: str):
    # Bracketed paste keeps embedded newlines as newlines instead of Enter.
    # Named buffer prevents concurrent sends in the brigade from clobbering
    # each other's payload between load and paste.
    # Two-phase poll-then-single-Enter. Phase 1: wait for proof the paste
    # started landing (a "[Pasted " collapse stub, or a head marker from the
    # payload). Phase 2: wait for the pane to stop repainting before Enter.
    # For long pastes Ink renders the stub from the FIRST chunk while the
    # rest still streams in, so firing on first-stub races the remainder and
    # the message never submits (stub + uncollapsed literal tail, stuck at
    # the prompt). Settling the pane closes that race. See notes/collapsed-
    # paste-mechanism-report.md + notes/brief-send-keys-stable-poll.md.
    target = f"{session}:{window}"
    buf = f"kitchen-{os.getpid()}-{next(_buffer_counter)}"
    baseline = capture_pane(session, window) or ""
    tmux("load-buffer", "-b", buf, "-", input=text, check=True)
    tmux("paste-buffer", "-b", buf, "-d", "-p", "-t", target, check=True)
    head = next((s for s in (line.strip() for line in text.splitlines()) if s), "")[:24]
    deadline = time.time() + 2.0
    signalled = False
    prev = None
    stable = 0
    while time.time() < deadline:
        time.sleep(0.05)
        pane = capture_pane(session, window) or ""
        if not signalled:
            if (pane.count("[Pasted ") > baseline.count("[Pasted ")
                    or (head and pane.count(head) > baseline.count(head))):
                signalled = True
                prev = pane
            continue
        if pane == prev:
            stable += 1
            if stable >= 3:  # ~150 ms of no repaint = paste fully landed
                break
        else:
            stable = 0
            prev = pane
    else:
        print(f"send_keys: paste not observed within 2.0s on {target}; "
              f"sending Enter anyway", file=sys.stderr)
    tmux("send-keys", "-t", target, "Enter", check=True)


