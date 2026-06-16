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


