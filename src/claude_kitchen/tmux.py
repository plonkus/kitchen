"""tmux helpers for claude-kitchen."""
import itertools
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
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
    # Claude shows a one-time "trust this folder?" dialog when launched in a
    # directory it hasn't seen — e.g. the detached overview sous's fresh state
    # dir, where no head chef is attached to confirm it. Accept the default
    # ("Yes, I trust this folder") with Enter so headless souses can boot.
    trust_dismissed = False
    while time.time() < deadline:
        content = capture_pane(session, window)
        if content and marker in content:
            return True
        if (backend == "claude" and not trust_dismissed and content
                and "trust this folder" in content):
            tmux("send-keys", "-t", f"{session}:{window}", "Enter", check=True)
            trust_dismissed = True
        if (backend == "codex" and not update_dismissed and content
                and ("Update available!" in content
                     or "Press enter to continue" in content)):
            tmux("send-keys", "-t", f"{session}:{window}", "3", "Enter",
                 check=True)
            update_dismissed = True
        time.sleep(1)
    return False


def _utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_json_line(log_dir: Path, filename: str, entry: dict):
    """Append one JSON line to <log_dir>/<filename>. Logging failures are
    swallowed — they must never shadow the original send_keys exception
    or break a ticket on disk-full / EACCES."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / filename).open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def send_keys(session: str, window: str, text: str,
              backend: Optional[str] = None, log_dir: Optional[Path] = None):
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
    #
    # Instrumentation (brief-send-keys-instrumentation.md): on exception or
    # deadline-without-signal, append a JSON line to
    # <log_dir>/<window>.send_keys.log. For codex backend specifically, also
    # snapshot pane pre/post the trailing Enter into
    # <log_dir>/<window>.send_keys_trace.log — used to diagnose the Ratatui
    # paste-mode-commit race against the trailing Enter (which Ink's
    # progressive redraw mitigation does NOT cover).
    start = time.time()
    target = f"{session}:{window}"
    signalled = False
    stable = 0
    baseline = ""
    post_paste = ""

    try:
        buf = f"kitchen-{os.getpid()}-{next(_buffer_counter)}"
        baseline = capture_pane(session, window) or ""
        tmux("load-buffer", "-b", buf, "-", input=text, check=True)
        tmux("paste-buffer", "-b", buf, "-d", "-p", "-t", target, check=True)
        head = next((s for s in (line.strip() for line in text.splitlines()) if s), "")[:24]
        deadline = time.time() + 2.0
        prev = None
        while time.time() < deadline:
            time.sleep(0.05)
            pane = capture_pane(session, window) or ""
            post_paste = pane
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
            if log_dir:
                _append_json_line(log_dir, f"{window}.send_keys.log", {
                    "ts": _utc_iso(),
                    "target": target,
                    "backend": backend,
                    "outcome": "deadline_no_signal",
                    "exception_type": None,
                    "exception_str": None,
                    "baseline_tail_200": baseline[-200:],
                    "post_paste_tail_200": post_paste[-200:],
                    "signalled": signalled,
                    "stable": stable,
                    "elapsed_ms": int((time.time() - start) * 1000),
                })

        if backend == "codex" and log_dir:
            pre_enter = capture_pane(session, window) or ""
            tmux("send-keys", "-t", target, "Enter", check=True)
            time.sleep(0.25)
            post_enter = capture_pane(session, window) or ""
            _append_json_line(log_dir, f"{window}.send_keys_trace.log", {
                "ts": _utc_iso(),
                "target": target,
                "pre_enter_tail_300": pre_enter[-300:],
                "post_enter_tail_300": post_enter[-300:],
                "elapsed_ms": int((time.time() - start) * 1000),
            })
        else:
            tmux("send-keys", "-t", target, "Enter", check=True)

    except Exception as e:
        if log_dir:
            _append_json_line(log_dir, f"{window}.send_keys.log", {
                "ts": _utc_iso(),
                "target": target,
                "backend": backend,
                "outcome": "exception",
                "exception_type": type(e).__name__,
                "exception_str": str(e),
                "baseline_tail_200": baseline[-200:],
                "post_paste_tail_200": post_paste[-200:],
                "signalled": signalled,
                "stable": stable,
                "elapsed_ms": int((time.time() - start) * 1000),
            })
        raise


