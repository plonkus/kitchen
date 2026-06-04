"""Tests for tmux helpers. Uses mocked subprocess calls."""
import subprocess
from unittest.mock import patch, MagicMock
import pytest
from claude_kitchen.tmux import (
    mc, bare, list_sessions, list_windows,
    has_session, capture_pane, wait_for_prompt,
)


class TestWaitForPrompt:
    @patch("claude_kitchen.tmux.time.sleep", lambda *_: None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_dismisses_claude_trust_dialog(self, mock_cap, mock_tmux):
        # The detached overview sous boots in a fresh dir → Claude's one-time
        # "trust this folder?" dialog. wait_for_prompt must confirm it (Enter)
        # and then succeed once the welcome marker appears.
        mock_cap.side_effect = [
            "Is this a project you created or one you trust this folder?",
            "Claude Code v2.1.162  Welcome back",
        ]
        assert wait_for_prompt("ck-overview", "sous", "claude", timeout=5) is True
        # Confirmed the dialog with a single Enter to the sous window.
        assert any(c.args[:1] == ("send-keys",) and "Enter" in c.args
                   for c in mock_tmux.call_args_list)

    @patch("claude_kitchen.tmux.time.sleep", lambda *_: None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_no_send_when_prompt_already_present(self, mock_cap, mock_tmux):
        mock_cap.return_value = "Claude Code v2.1.162"
        assert wait_for_prompt("ck-overview", "sous", "claude", timeout=5) is True
        mock_tmux.assert_not_called()

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


