"""tmux helpers for claude-kitchen."""
import itertools
import os
import re
import subprocess
import time
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

# Backends whose busy marker is NOT shown for the whole turn. This Claude build
# renders the spinner only at turn start, then streams text under a footer
# byte-identical to idle — so a marker-miss is inconclusive and pane_busy falls
# back to change-detection. Every OTHER backend shows a persistent busy footer
# (codex "esc to interrupt", gemini "esc to cancel"), so for them a marker-miss
# is authoritatively idle — no fallback, no sleep.
_NO_STREAMING_MARKER = {"claude"}


def _pane_tail(session: str, window: str) -> list[str]:
    """Last 40 non-blank lines of the pane — the busy/idle comparison surface
    for pane_busy."""
    content = capture_pane(session, window) or ""
    return [ln for ln in content.splitlines() if ln.strip()][-40:]


def _busy_marker_hit(tail: list[str], backend: str) -> bool:
    """True if `backend`'s busy marker matches any of the last ~6 non-blank tail
    lines. Raises ValueError on an unknown backend (e.g. a mutated status file)
    so the bad value is diagnosable rather than a raw KeyError."""
    try:
        pattern = _BUSY_MARKERS[backend]
    except KeyError:
        raise ValueError(f"pane_busy: unknown backend {backend!r}")
    return any(re.search(pattern, ln, re.IGNORECASE) for ln in tail[-6:])


def pane_busy(session: str, window: str, backend: str, settle: float = 0.35) -> bool:
    """True if the cook is mid-turn, False if idle at the prompt (or empty pane).

    Contract: `True` means the backend's busy MARKER is present, OR — for a
    backend with no persistent streaming marker (claude) — the pane is actively
    REPAINTING. So `True` is NOT always "the backend emitted a busy footer": a
    streaming claude reports busy purely from its growing transcript. A consumer
    (B1) must not assume a footer was seen.

    Resolution:
      1. busy marker in the last ~6 non-blank tail lines → True immediately, no
         sleep (covers codex/gemini for the whole turn, claude at turn start).
      2. marker-miss:
         - markerless backend (claude): capture a second tail ~`settle` later;
           busy iff it differs from the first. Required because a streaming
           claude's footer is byte-identical to idle — the only live signal is
           the growing transcript. Idle panes are stable across `settle`
           (verified), so this does not false-positive.
         - any other backend: False, with NO extra sleep — its busy footer is
           persistent, so a marker-miss is authoritatively idle.

    `backend` is REQUIRED (markers are backend-specific and tmux.py has no
    state-dir context to infer it) — callers pass it (send_keys already has
    `backend`; peek reads it from the cook's state file). Unknown backend raises
    ValueError."""
    first = _pane_tail(session, window)
    if _busy_marker_hit(first, backend):
        return True
    if backend not in _NO_STREAMING_MARKER:
        return False
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


# Verified-submit loop tuning. One Enter usually submits; the extra attempts
# recover codex's paste-mode-commit race (a swallowed first Enter). An extra
# Enter into an already-submitted/empty composer is a verified no-op, so
# retrying is safe (IRON RULE: retry Enter ONLY, never re-paste).
_SUBMIT_ATTEMPTS = 8
_SUBMIT_POLL = 0.3


def _cursor_row(session: str, window: str) -> str:
    """The composer/input row the cursor sits on, stripped. After a paste this
    holds the ticket payload (its tail line for codex/gemini, or an Ink
    "[Pasted N lines]" stub for claude); after a successful submit the composer
    clears and this no longer holds it. Routed through the tmux() wrapper so the
    per-call TIMEOUT applies."""
    target = f"{session}:{window}"
    cy = tmux("display-message", "-t", target, "-p", "#{cursor_y}").stdout.strip()
    if not cy:
        return ""
    return tmux("capture-pane", "-t", target, "-p", "-S", cy, "-E", cy).stdout.strip()


def send_keys(session: str, window: str, text: str, backend: Optional[str] = None):
    # Bracketed paste keeps embedded newlines as newlines instead of Enter.
    # Named buffer prevents concurrent sends in the brigade from clobbering
    # each other's payload between load and paste.
    #
    # Phase 1 — settle-poll: wait for proof the paste landed (a "[Pasted "
    # collapse stub, or a head marker from the payload) AND for the pane to stop
    # repainting. For long pastes Ink renders the stub from the FIRST chunk
    # while the rest still streams in, so submitting on first-stub races the
    # remainder and the message never submits. Settling closes that race. See
    # notes/collapsed-paste-mechanism-report.md + notes/brief-send-keys-stable-
    # poll.md. If the paste is never positively observed by the deadline we
    # FAIL CLEARLY (raise) rather than submit blind into an unknown composer.
    #
    # Phase 2 — verified submit: loop Enter until the ticket leaves the composer
    # (a positive transition vs the pre-loop baseline), which closes codex's
    # paste-mode-commit race against the trailing Enter. Enter-only — NEVER
    # re-paste, or a swallowed Enter would duplicate the text in the composer.
    target = f"{session}:{window}"
    signalled = False
    stable = 0

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

    if not signalled:
        raise RuntimeError(
            f"send_keys: paste not observed within 2.0s on {target}; refusing "
            f"to submit into an unknown composer")

    # The composer now holds the payload — capture it as the pre-loop baseline,
    # then Enter until that payload leaves the composer (turn started, or the
    # message queued behind a busy turn — either way it was submitted).
    composer = _cursor_row(session, window)
    for _ in range(_SUBMIT_ATTEMPTS):
        tmux("send-keys", "-t", target, "Enter", check=True)
        time.sleep(_SUBMIT_POLL)
        if composer and composer not in _cursor_row(session, window):
            return
    raise RuntimeError(
        f"send_keys: ticket never left the composer on {target} after "
        f"{_SUBMIT_ATTEMPTS} Enter attempts; submission not confirmed")


