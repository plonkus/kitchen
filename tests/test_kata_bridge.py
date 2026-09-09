"""Tests for the kata event bridge."""
import asyncio
from pathlib import Path

from claude_kitchen.kata_bridge import _sentence

WORKSPACE = Path("/nonexistent")  # no lookup happens in these cases


def sentence(event):
    return asyncio.run(_sentence(WORKSPACE, event))


def _created(actor="patrick", title="Ship the ledger"):
    return {
        "type": "issue.created",
        "actor": actor,
        "issue_short_id": "abc4",
        "payload": {"title": title, "body": "", "status": "open"},
    }


class TestSentence:
    def test_reads_as_one_plain_sentence(self):
        assert sentence(_created()) == 'patrick filed "Ship the ledger"'

    def test_drops_the_souss_own_writes(self):
        """The sous writes as `sous`; its own events come back down the tail."""
        assert sentence(_created(actor="sous")) is None

    def test_drops_events_the_sous_cannot_act_on(self):
        assert sentence({"type": "project.created", "actor": "patrick",
                         "payload": {"name": "kitchen"}}) is None

    def test_collapses_multiline_prose_to_one_line(self):
        event = _created(title="Ship\nthe   ledger")
        assert sentence(event) == 'patrick filed "Ship the ledger"'
