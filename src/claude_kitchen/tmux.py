"""tmux helpers for claude-kitchen."""
import itertools
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Per-call tmux timeout. Generous enough that fast queries (has-session,
# new-session, list-windows) don't spuriously time out when the tmux server is
# briefly serialized behind another kitchen launching at the same time — the
# `kitchen open --sub-sous` x2 race that used to crash both opens at 5s.
TIMEOUT = 15
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
    "gemini": "? for shortcuts",  # agy TUI footer, present when input is editable
}

# Busy markers: a regex present in the pane tail while the agent is mid-turn,
# gone when it's idle at the prompt. Backend-specific and confirmed empirically
# against real cooks (matched case-insensitively against each non-blank tail
# line). See notes/brief-chunk1-b3 evidence for the captured pane tails.
#
#   claude — the working spinner: a rotating asterisk glyph (U+2722–U+273F, or
#     "·"/"*") followed by a gerund and a "…" ellipsis, e.g. "✻ Crafting…",
#     "✶ Fluttering… (1s · ↓ 1 tokens)". This Claude build shows NO "esc to
#     interrupt" text (verified absent). The ellipsis is what separates the live
#     spinner from the idle completion line "✻ Churned for 54s" (same glyph, no
#     "…") — so the ellipsis is required, not just the glyph.
#   codex — the "esc to interrupt" footer shown while a turn runs
#     ("• Working (2s • esc to interrupt)").
#   gemini — the "esc to cancel" footer shown while an agy turn runs (alongside
#     a "⣾ Working..." spinner); confirmed against a real agy cook.
_BUSY_MARKERS = {
    "claude": r"[·*✢-✿]\s.*…",
    "codex": r"esc to interrupt",
    "gemini": r"esc to (cancel|interrupt)",
}


def _pane_tail(session: str, window: str) -> list[str]:
    """Last 40 non-blank lines of the pane — the busy/idle comparison surface
    for pane_busy."""
    content = capture_pane(session, window) or ""
    return [ln for ln in content.splitlines() if ln.strip()][-40:]


def pane_busy(session: str, window: str, backend: str, settle: float = 0.35) -> bool:
    """True if the cook is mid-turn, False if idle at the prompt (or empty pane).

    Two OR'd signals — a genuinely idle pane has neither:
      1. the backend's busy marker is present in the last ~6 non-blank tail lines
         (codex's persistent "esc to interrupt" footer; claude's start-of-turn
         spinner). Instant, no sleep — covers codex for its whole turn.
      2. the pane is actively REPAINTING (two tails captured ~`settle` apart
         differ). This is required for a claude cook STREAMING a long reply:
         this Claude build shows the spinner only at turn start, and during text
         streaming the footer is byte-identical to idle — the only live signal is
         the growing transcript. Verified: idle panes are stable across `settle`,
         so this does not false-positive.

    `backend` is REQUIRED — markers are backend-specific and tmux.py has no
    state-dir context to infer it; callers pass it (send_keys already has
    `backend`; peek reads it from the cook's state file)."""
    first = _pane_tail(session, window)
    if any(re.search(_BUSY_MARKERS[backend], ln, re.IGNORECASE)
           for ln in first[-6:]):
        return True
    time.sleep(settle)
    return _pane_tail(session, window) != first


def wait_for_prompt(session: str, window: str, backend: str,
                    timeout: int = 180, stall_timeout: int = 45) -> bool:
    """Wait until the agent's welcome banner appears, then return True.

    Progress-based, not a flat cap: under heavy machine load the whole boot
    (window → dialog → confirm → render the banner) can take far longer than any
    fixed deadline — diagnosed live on a load-50 box, where a healthy child sous
    simply booted slowly. So keep waiting as long as the pane keeps CHANGING
    (making progress) and give up only after `stall_timeout` seconds of NO change
    (truly stuck / crashed) or the `timeout` hard ceiling. Returns False on
    either give-up condition.
    """
    marker = _PROMPT_MARKERS[backend]
    hard_deadline = time.time() + timeout
    # Codex shows an "Update available!" picker before its welcome banner when a
    # new version is published. Dismiss it once with `3` (= "Skip until next
    # version") + Enter so the welcome marker can appear.
    update_dismissed = False
    # A claude agent launched with --dangerously-load-development-channels (the
    # sous) shows a one-time "Loading development channels" confirmation before
    # its welcome banner. Option 1 ("I am using this for local development") is
    # pre-selected, so a bare Enter confirms it. Only the sous loads dev
    # channels, so this never fires for cooks.
    channels_confirmed = False
    last = None
    last_change = time.time()
    while time.time() < hard_deadline:
        # tmux can stall briefly when another kitchen is launching at the same
        # moment; a TimeoutExpired here is transient, not fatal — swallow it and
        # retry on the next tick instead of crashing the open mid-flight.
        try:
            content = capture_pane(session, window)
            if content:
                if marker in content:
                    return True
                if (backend == "codex" and not update_dismissed
                        and ("Update available!" in content
                             or "Press enter to continue" in content)):
                    tmux("send-keys", "-t", f"{session}:{window}", "3", "Enter",
                         check=True)
                    update_dismissed = True
                if (backend == "claude" and not channels_confirmed
                        and "Loading development channels" in content):
                    tmux("send-keys", "-t", f"{session}:{window}", "Enter", check=True)
                    channels_confirmed = True
                if content != last:
                    last, last_change = content, time.time()
                elif time.time() - last_change > stall_timeout:
                    return False  # pane frozen → stuck/crashed, not slow boot
        except subprocess.TimeoutExpired:
            pass
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


