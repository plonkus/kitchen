"""Tests for the codex sous bridge.

Only the parts a live run can't cheaply prove: the claude-only guards (which
must fire before cmd_open mutates anything) and push's start-vs-steer choice.
The app-server handshake, thread/start and the resume attach are covered by the
end-to-end run, not by mocks of a protocol we don't own."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from claude_kitchen.cli import cmd_open
from claude_kitchen.codex_sous import Sous, _format


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


class FakeClient:
    """Stands in for a subscribed connection: records calls, replays notifications."""

    def __init__(self, notes=()):
        self.notes = list(notes)
        self.calls = []

    async def call(self, method, params=None, timeout=120):
        self.calls.append((method, params))
        return {}


def _started(turn_id):
    return {"method": "turn/started", "params": {"turn": {"id": turn_id}}}


def _completed(turn_id):
    return {"method": "turn/completed", "params": {"turn": {"id": turn_id}}}


class TestInFlightTurn:
    def test_no_turns_seen_means_idle(self):
        assert Sous(FakeClient(), "th").in_flight_turn() is None

    def test_started_then_completed_is_idle(self):
        c = FakeClient([_started("t1"), _completed("t1")])
        assert Sous(c, "th").in_flight_turn() is None

    def test_started_without_completed_is_in_flight(self):
        c = FakeClient([_started("t1"), _completed("t1"), _started("t2")])
        assert Sous(c, "th").in_flight_turn() == "t2"

    def test_notifications_arriving_later_are_picked_up(self):
        c = FakeClient()
        sous = Sous(c, "th")
        assert sous.in_flight_turn() is None
        c.notes.append(_started("t9"))
        assert sous.in_flight_turn() == "t9"


class TestPush:
    def test_idle_sous_gets_a_new_turn(self):
        c = FakeClient()
        asyncio.run(Sous(c, "th").push("hello"))
        method, params = c.calls[0]
        assert method == "turn/start"
        assert params["input"][0]["text"] == "hello"

    def test_busy_sous_gets_steered_into_the_running_turn(self):
        c = FakeClient([_started("t2")])
        asyncio.run(Sous(c, "th").push("hello"))
        method, params = c.calls[0]
        assert method == "turn/steer"
        assert params["expectedTurnId"] == "t2"
