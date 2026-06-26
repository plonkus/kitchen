"""Tests for tmux helpers. Uses mocked subprocess calls."""
import subprocess
from unittest.mock import patch, MagicMock
import pytest
from claude_kitchen.tmux import (
    mc, bare, list_sessions, list_windows,
    has_session, capture_pane,
)

CK_PREFIX = "ck-"


def _mock_run(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


class TestNaming:
    def test_mc_adds_prefix(self):
        assert mc("risotto") == "ck-risotto"

    def test_mc_idempotent(self):
        assert mc("ck-risotto") == "ck-risotto"

    def test_bare_strips_prefix(self):
        assert bare("ck-risotto") == "risotto"

    def test_bare_noop_without_prefix(self):
        assert bare("risotto") == "risotto"


class TestListSessions:
    @patch("claude_kitchen.tmux.tmux")
    def test_filters_ck_sessions(self, mock_tmux):
        mock_tmux.return_value = _mock_run("ck-risotto\nck-bolognese\nother\n")
        result = list_sessions()
        assert result == ["ck-risotto", "ck-bolognese"]

    @patch("claude_kitchen.tmux.tmux")
    def test_empty_on_failure(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=1)
        assert list_sessions() == []


class TestListWindows:
    @patch("claude_kitchen.tmux.tmux")
    def test_returns_window_names(self, mock_tmux):
        mock_tmux.return_value = _mock_run("sous\neng\nreviewer\n")
        result = list_windows("ck-risotto")
        assert result == ["sous", "eng", "reviewer"]

    @patch("claude_kitchen.tmux.tmux")
    def test_propagates_tmux_failure(self, mock_tmux):
        # list_windows passes check=True so tmux errors surface, not silently
        # degrade to []. Critical for _sweep_cooks — an empty list means "no
        # live cooks," a tmux failure must not be conflated with that.
        mock_tmux.side_effect = subprocess.CalledProcessError(1, "tmux")
        with pytest.raises(subprocess.CalledProcessError):
            list_windows("ck-missing")


class TestHasSession:
    @patch("claude_kitchen.tmux.tmux")
    def test_true_when_exists(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=0)
        assert has_session("ck-risotto") is True

    @patch("claude_kitchen.tmux.tmux")
    def test_false_when_missing(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=1)
        assert has_session("ck-risotto") is False


class TestCapturePane:
    @patch("claude_kitchen.tmux.tmux")
    def test_captures_output(self, mock_tmux):
        mock_tmux.return_value = _mock_run("hello world\n❯ ")
        result = capture_pane("ck-risotto", "eng")
        assert "hello world" in result

    @patch("claude_kitchen.tmux.tmux")
    def test_returns_none_on_failure(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=1)
        assert capture_pane("ck-risotto", "eng") is None


class TestWaitForPrompt:
    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_confirms_dev_channels_dialog_for_claude(self, mock_cap, mock_tmux, mock_sleep):
        """A claude sous launched with --dangerously-load-development-channels
        shows a confirmation dialog before its welcome banner; wait_for_prompt
        must auto-confirm it (bare Enter on the pre-selected option 1)."""
        from claude_kitchen.tmux import wait_for_prompt
        mock_cap.side_effect = [
            "WARNING: Loading development channels\n  ❯ 1. I am using this for local development",
            "Claude Code v2.1.120",
        ]
        assert wait_for_prompt("ck-x", "sous", "claude", timeout=5) is True
        sent = [c.args for c in mock_tmux.call_args_list]
        assert ("send-keys", "-t", "ck-x:sous", "Enter") in sent

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_no_dialog_no_keystrokes_for_claude(self, mock_cap, mock_tmux, mock_sleep):
        """Welcome banner already up → no dialog → no keystrokes sent."""
        from claude_kitchen.tmux import wait_for_prompt
        mock_cap.side_effect = ["Claude Code v2.1.120"]
        assert wait_for_prompt("ck-x", "sous", "claude", timeout=5) is True
        mock_tmux.assert_not_called()

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_tolerates_transient_tmux_timeout(self, mock_cap, mock_tmux, mock_sleep):
        """A TimeoutExpired from tmux under launch load is transient: the wait
        loop must swallow it and keep polling, not crash the open."""
        from claude_kitchen.tmux import wait_for_prompt
        mock_cap.side_effect = [
            subprocess.TimeoutExpired(cmd="tmux", timeout=15),
            "Claude Code v2.1.120",
        ]
        assert wait_for_prompt("ck-x", "sous", "claude", timeout=5) is True

    @patch("claude_kitchen.tmux.time")
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_gives_up_on_stall_not_full_ceiling(self, mock_cap, mock_tmux, mock_time):
        """An unchanging pane (no marker) is truly stuck → give up after
        stall_timeout, WITHOUT waiting the (much larger) hard ceiling."""
        from claude_kitchen.tmux import wait_for_prompt
        clock = [0]
        def now():
            clock[0] += 10
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.return_value = "booting… (no banner, frozen)"
        # Hard ceiling 100000 so the STALL (not the ceiling) is what returns.
        assert wait_for_prompt("ck-x", "sous", "claude",
                               timeout=100000, stall_timeout=45) is False
        assert clock[0] < 1000, "should give up at the stall, not crawl to the ceiling"

    @patch("claude_kitchen.tmux.time")
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_slow_but_progressing_boot_reaches_prompt(self, mock_cap, mock_tmux, mock_time):
        """A pane that keeps CHANGING is making progress (slow boot under load):
        the wait must NOT give up before the banner finally appears — even across
        many ticks that a flat short cap would have killed."""
        from claude_kitchen.tmux import wait_for_prompt
        clock = [0]
        def now():
            clock[0] += 10  # 10s per call → tens of seconds pass between frames
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        # Distinct frames (progress) for a long time, then the welcome marker.
        mock_cap.side_effect = [f"boot frame {i}" for i in range(8)] + ["Claude Code v2.1.178"]
        assert wait_for_prompt("ck-x", "sous", "claude",
                               timeout=100000, stall_timeout=45) is True


# Real pane captures from the B3 live verification (claude+codex+gemini cooks,
# ck-sbusy). These are the empirical fixtures the busy/idle logic is pinned to.
CLAUDE_IDLE = (
    "  By making information abundant rather than scarce, it reshaped society.\n"
    "✻ Churned for 54s\n"
    "────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────\n"
    "  ██░░░░░░░░░░ 4%  claude-opus-4-8[1m]  main\n"
    "  [ tmux attach -t ck-sbusy ]  [ 0/2 agents active ]\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
# Turn-start spinner (rotating asterisk glyph + gerund + ellipsis).
CLAUDE_BUSY_SPINNER = (
    "  the copying of words but fundamentally restructured knowledge.\n"
    "✻ Churned for 54s\n"
    "❯ Now, without tools, write another 20-paragraph essay.\n"
    "✽ Crafting…\n"
)
CLAUDE_BUSY_SPINNER_SUFFIX = "✶ Fluttering… (1s · ↓ 1 tokens)\n"
# Two frames of a claude STREAMING text: no spinner, footer byte-identical to
# idle — only the growing transcript differs.
CLAUDE_STREAM_1 = (
    "  insulator, it became pliable when heated and hard when cooled.\n"
    "────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────\n"
    "  ██░░░░░░░░░░ 4%  claude-opus-4-8[1m]  main\n"
    "  [ tmux attach -t ck-sbusy ]  [ 1/2 agents active ]\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
CLAUDE_STREAM_2 = CLAUDE_STREAM_1.replace(
    "  insulator, it became pliable when heated and hard when cooled.\n",
    "  insulator, it became pliable when heated and hard when cooled.\n"
    "  The Gutta Percha Company in London became a vital supplier.\n",
)
CODEX_IDLE = (
    "  making radio one of the most adaptable communication technologies.\n"
    "› Explain this codebase\n"
    "  gpt-5.5 high fast · /tmp/proj · Context 98% left\n"
)
CODEX_BUSY = (
    "  making radio one of the most adaptable communication technologies.\n"
    "› Without tools, write a 25-paragraph essay on the history of clocks.\n"
    "• Working (2s • esc to interrupt)\n"
    "› Explain this codebase\n"
    "  gpt-5.5 high fast · /tmp/proj · Context 98% left\n"
)
GEMINI_IDLE = (
    "  It continues to be the ultimate symbol of direction and orientation.\n"
    ">\n"
    "? for shortcuts                                  Gemini 3.5 Flash (Medium)\n"
)
GEMINI_BUSY = (
    "> Without tools, write a 20-paragraph essay on the history of compasses.\n"
    "⣻  Working...\n"
    ">\n"
    "esc to cancel                                    Gemini 3.5 Flash (Medium)\n"
)


def _tail(raw):
    """Mirror _pane_tail's filtering so marker tests can run on raw captures."""
    return [ln for ln in raw.splitlines() if ln.strip()][-40:]


class TestBusyMarkerHit:
    """Pure marker-match logic against the real captured tails."""

    def test_claude_spinner_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CLAUDE_BUSY_SPINNER), "claude") is True

    def test_claude_spinner_with_token_suffix_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CLAUDE_BUSY_SPINNER_SUFFIX), "claude") is True

    def test_claude_idle_completion_line_does_NOT_match(self):
        # "✻ Churned for 54s" has the same glyph as the live spinner but no "…".
        # The ellipsis is the discriminator — this is the brittle bit, pin it.
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(["✻ Churned for 54s"], "claude") is False

    def test_claude_idle_footer_does_NOT_match(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CLAUDE_IDLE), "claude") is False

    def test_codex_working_footer_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CODEX_BUSY), "codex") is True

    def test_codex_idle_does_NOT_match(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CODEX_IDLE), "codex") is False

    def test_gemini_cancel_footer_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(GEMINI_BUSY), "gemini") is True

    def test_gemini_idle_does_NOT_match(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(GEMINI_IDLE), "gemini") is False

    def test_unknown_backend_raises_valueerror(self):
        from claude_kitchen.tmux import _busy_marker_hit
        with pytest.raises(ValueError, match="unknown backend"):
            _busy_marker_hit(["whatever"], "llama")


class TestPaneBusy:
    """Full pane_busy: marker fast-path, claude change-detection, codex/gemini
    no-fallback, unknown-backend error. capture_pane mocked — no live tmux."""

    @patch("claude_kitchen.tmux.capture_pane")
    def test_codex_busy_marker_is_instant_single_capture(self, mock_cap):
        # Marker hit → True with exactly ONE capture (no settle, no fallback).
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CODEX_BUSY
        assert pane_busy("ck-x", "cx", "codex") is True
        assert mock_cap.call_count == 1

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_codex_idle_no_fallback_single_capture(self, mock_cap, mock_sleep):
        # Persistent-footer backend: marker-miss is authoritatively idle —
        # exactly ONE capture, and NO settle sleep.
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CODEX_IDLE
        assert pane_busy("ck-x", "cx", "codex") is False
        assert mock_cap.call_count == 1
        mock_sleep.assert_not_called()

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_gemini_idle_no_fallback_single_capture(self, mock_cap, mock_sleep):
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = GEMINI_IDLE
        assert pane_busy("ck-x", "gm", "gemini") is False
        assert mock_cap.call_count == 1
        mock_sleep.assert_not_called()

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_claude_idle_stable_pane_is_not_busy(self, mock_cap, mock_sleep):
        # Markerless backend, marker-miss → change-detection. Idle pane is stable
        # across the settle window, so the two captures match → False (no
        # false-positive). Two captures taken.
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CLAUDE_IDLE
        assert pane_busy("ck-x", "cc", "claude") is False
        assert mock_cap.call_count == 2

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_claude_streaming_detected_by_change(self, mock_cap, mock_sleep):
        # Markerless backend streaming text: no spinner, footer identical to
        # idle, but the transcript grows between captures → busy.
        from claude_kitchen.tmux import pane_busy
        mock_cap.side_effect = [CLAUDE_STREAM_1, CLAUDE_STREAM_2]
        assert pane_busy("ck-x", "cc", "claude") is True
        assert mock_cap.call_count == 2

    @patch("claude_kitchen.tmux.capture_pane")
    def test_claude_spinner_marker_is_instant(self, mock_cap):
        # Spinner present → True via the marker fast-path, single capture.
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CLAUDE_BUSY_SPINNER
        assert pane_busy("ck-x", "cc", "claude") is True
        assert mock_cap.call_count == 1

    @patch("claude_kitchen.tmux.capture_pane")
    def test_unknown_backend_raises_valueerror(self, mock_cap):
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CLAUDE_IDLE
        with pytest.raises(ValueError, match="unknown backend"):
            pane_busy("ck-x", "cc", "llama")



# ---- B1: verified-submit loop ----
# send_keys: paste (untouched) → settle-poll → Enter-until-the-payload-leaves-
# the-composer. These drive the real loop logic with capture_pane (settle) and
# tmux() (cursor row + Enter) mocked — no live tmux. They pin: a swallowed first
# Enter is recovered by retry, success is keyed on the composer clearing (not a
# busy level), and both deadlines fail clearly (raise) instead of submitting
# blind or silently returning.

_SETTLE_HEAD = "DOTHING"  # ticket text; head == text so it's easy to embed


def _settle_capture():
    """capture_pane side-effect for the settle-poll: a baseline with NO head,
    then stable panes that DO contain the head (so paste is positively observed
    and the pane settles)."""
    calls = {"n": 0}

    def cap(session, window, full=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return "idle transcript\n❯ "          # baseline, no head
        return f"transcript line\n❯ {_SETTLE_HEAD} landed\n"  # head present, stable

    return cap


def _no_paste_capture(session, window, full=False):
    """Settle-poll never sees the head → paste never positively observed."""
    return "idle transcript, head never appears\n❯ "


def _tmux_with_composer(rows):
    """tmux() side-effect. display-message → a cursor row index; capture-pane →
    the next scripted composer-row string (the cursor row send_keys reads to
    decide if the ticket left the composer); send-keys/load-buffer/paste-buffer
    → success. Records how many Enters were sent."""
    queue = list(rows)
    state = {"enter": 0}

    def fake(*args, **kwargs):
        cmd = args[0]
        m = MagicMock(returncode=0, stdout="")
        if cmd == "display-message":
            m.stdout = "20\n"
        elif cmd == "capture-pane":
            m.stdout = (queue.pop(0) if queue else "") + "\n"
        elif cmd == "send-keys":
            state["enter"] += 1
        return m

    fake.state = state
    return fake


class TestSendKeysVerifiedSubmit:
    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_first_enter_submits(self, mock_cap, mock_tmux, mock_sleep):
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_capture()
        # composer: baseline holds the payload; after the 1st Enter it's cleared.
        fake = _tmux_with_composer(["PAYLOAD tail line", "❯ "])
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 1

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_swallowed_first_enter_is_retried(self, mock_cap, mock_tmux, mock_sleep):
        # The codex paste-commit race: the 1st Enter is swallowed (composer still
        # holds the payload), the 2nd lands (composer clears). The loop recovers.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_capture()
        fake = _tmux_with_composer(["PAYLOAD tail line",   # baseline
                                    "PAYLOAD tail line",   # after Enter #1: swallowed
                                    "❯ "])            # after Enter #2: submitted
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 2

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_never_clears_raises_after_all_attempts(self, mock_cap, mock_tmux, mock_sleep):
        # Composer never clears → no positive transition → raise (never silently
        # return "delivered"). This is also the already-busy guard: a busy level
        # alone is NOT accepted as success; only the composer clearing is.
        from claude_kitchen.tmux import send_keys, _SUBMIT_ATTEMPTS
        mock_cap.side_effect = _settle_capture()
        fake = _tmux_with_composer(["PAYLOAD tail line"] * (1 + _SUBMIT_ATTEMPTS))
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="never left the composer"):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == _SUBMIT_ATTEMPTS

    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_paste_never_observed_raises_without_any_enter(self, mock_time, mock_cap, mock_tmux):
        # Settle deadline with no paste-landed signal → fail clearly (raise),
        # and CRUCIALLY send no Enter — never submit blind into an unknown
        # composer (replaces the old "sending Enter anyway").
        from claude_kitchen.tmux import send_keys
        clock = [0.0]
        def now():
            clock[0] += 1.0
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.side_effect = _no_paste_capture
        fake = _tmux_with_composer([])
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="paste not observed"):
            send_keys("ck-x", "cx", "a ticket whose head never lands", backend="codex")
        assert fake.state["enter"] == 0

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_never_re_pastes_only_enters(self, mock_cap, mock_tmux, mock_sleep):
        # IRON RULE: retry is Enter-only. After the initial paste-buffer there
        # must be NO further load-buffer/paste-buffer, no matter how many Enter
        # retries the loop runs.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_capture()
        fake = _tmux_with_composer(["PAYLOAD tail line", "PAYLOAD tail line", "❯ "])
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        cmds = [c.args[0] for c in mock_tmux.call_args_list]
        assert cmds.count("paste-buffer") == 1
        assert cmds.count("load-buffer") == 1
        assert fake.state["enter"] == 2  # retried, but never re-pasted
