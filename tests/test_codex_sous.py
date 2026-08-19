"""Tests for the codex sous bridge.

Only the parts a live run can't cheaply prove: the claude-only guards (which
must fire before cmd_open mutates anything) and push's start-vs-steer choice.
The app-server handshake, thread/start and the resume attach are covered by the
end-to-end run, not by mocks of a protocol we don't own."""
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_kitchen.cli import cmd_open
from claude_kitchen.codex_sous import _format, push


def _codex_args(**over):
    args = MagicMock()
    args.backend = "codex"
    args.resume = False
    args.sub_sous = False
    for k, v in over.items():
        setattr(args, k, v)
    return args


class TestClaudeOnlyGuards:
    @patch("claude_kitchen.cli.resolve_project")
    def test_resume_rejected_before_any_mutation(self, mock_resolve):
        with pytest.raises(SystemExit) as e:
            cmd_open(_codex_args(resume=True))
        assert "claude-only" in str(e.value)
        # The guard is upstream of everything, including project resolution.
        mock_resolve.assert_not_called()

    @patch("claude_kitchen.cli.resolve_project")
    def test_sub_sous_rejected(self, mock_resolve):
        with pytest.raises(SystemExit) as e:
            cmd_open(_codex_args(sub_sous=True))
        assert "claude-only" in str(e.value)
        mock_resolve.assert_not_called()


class TestFormat:
    def test_renders_a_channel_tag(self):
        out = _format({"cook": "alpha", "ts": "2026-08-12T00:00:00Z",
                       "ctx": "61%", "summary": "done"})
        assert out == ('<channel cook="alpha" ts="2026-08-12T00:00:00Z" ctx="61%">\n'
                       'done\n</channel>')

    def test_omits_ctx_when_the_cook_has_no_token_info(self):
        assert 'ctx=' not in _format({"cook": "alpha", "ts": "t", "summary": "s"})


class TestBackendIsFixedAtOpen:
    """Reopening a codex kitchen as claude must refuse, not run both halves:
    the codex bridge still owns kitchen.sock, so the claude channel-server
    would stand down and the claude sous would hear nothing."""

    def _kitchen(self, tmp_path, stored):
        kj = {"source": "/tmp/myproject", "slug": "widget"}
        if stored:
            kj["backend"] = stored
        (tmp_path / "kitchen.json").write_text(json.dumps(kj))

    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp/myproject"))
    def _open(self, backend, tmp_path, mock_resolve, mock_ns, mock_state):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.name, args.project, args.worktree_path = "risotto", "/tmp/myproject", None
        args.resume = args.sub_sous = False
        args.backend = backend
        cmd_open(args)

    def test_claude_reopen_of_a_codex_kitchen_refuses(self, tmp_path):
        self._kitchen(tmp_path, "codex")
        with pytest.raises(SystemExit) as e:
            self._open("claude", tmp_path)
        assert "opened with --backend codex" in str(e.value)
        # and it refused BEFORE writing the claude MCP config, which is what
        # would have pointed a claude sous at a socket the bridge owns
        assert not (tmp_path / "kitchen-mcp.json").exists()

    def test_codex_reopen_of_a_claude_kitchen_refuses(self, tmp_path):
        self._kitchen(tmp_path, "claude")
        with pytest.raises(SystemExit) as e:
            self._open("codex", tmp_path)
        assert "opened with --backend claude" in str(e.value)

    def test_a_kitchen_from_before_the_flag_counts_as_claude(self, tmp_path):
        self._kitchen(tmp_path, None)
        with pytest.raises(SystemExit) as e:
            self._open("codex", tmp_path)
        assert "opened with --backend claude" in str(e.value)


class FakeClient:
    """Stands in for a live app-server connection: records calls."""

    def __init__(self):
        self.calls = []
        self.closed = False

    async def call(self, method, params=None, timeout=120):
        self.calls.append((method, params))
        return {}

    async def close(self):
        self.closed = True


class TestPush:
    def test_a_report_is_always_a_new_turn(self):
        """Never turn/steer: an idle sous starts a turn on the report and a busy
        one queues it, so there is no expectedTurnId race to lose a report to."""
        c = FakeClient()

        async def connect(port):
            return c

        with patch("claude_kitchen.codex_sous.Client.connect", new=connect):
            asyncio.run(push(1234, "th", "hello"))
        assert [m for m, _ in c.calls] == ["turn/start"]
        assert c.calls[0][1]["threadId"] == "th"
        assert c.calls[0][1]["input"][0]["text"] == "hello"
        assert c.closed, "the per-report connection must not leak"
