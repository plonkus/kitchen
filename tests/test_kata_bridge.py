"""Tests for the kata event bridge."""
import asyncio
from pathlib import Path

import pytest

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


def _updated(payload, actor="patrick"):
    return {"type": "issue.updated", "actor": actor,
            "issue_short_id": "abc4", "payload": payload}


class TestSentence:
    def test_reads_as_one_plain_sentence(self):
        assert sentence(_created()) == 'patrick filed "Ship the ledger"'

    def test_drops_the_souss_own_writes(self):
        """The sous writes as `sous`; its own events come back down the tail."""
        assert sentence(_created(actor="sous")) is None

    def test_collapses_multiline_prose_to_one_line(self):
        assert sentence(_created(title="Ship\nthe   ledger")) == \
            'patrick filed "Ship the ledger"'


class TestIssueUpdated:
    """One kata event type covers three different edits and carries only the
    fields the edit touched."""

    def test_title_edit_reads_as_a_rename(self):
        assert sentence(_updated({"old_title": "Old", "title": "New"})) == \
            'patrick renamed "Old" to "New"'

    def test_body_only_edit_reads_as_a_description_edit(self, monkeypatch):
        """The payload carries no title at all — the old code raised KeyError
        on old_title here — so the issue has to be looked up."""
        import claude_kitchen.kata_bridge as kb

        async def fake_show(workspace, *args):
            return {"issue": {"title": "Ship the ledger"}}

        monkeypatch.setattr(kb, "_kata", fake_show)
        event = _updated({"body": "New body.", "updated_at": "now"})
        assert sentence(event) == \
            'patrick edited the description of "Ship the ledger"'

    def test_title_and_body_together_reads_as_a_rename(self):
        event = _updated({"body": "b", "old_title": "Old", "title": "New"})
        assert sentence(event) == 'patrick renamed "Old" to "New"'


class TestEveryEventIsDelivered:
    def test_link_changes_are_delivered(self, monkeypatch):
        import claude_kitchen.kata_bridge as kb

        async def fake_show(workspace, *args):
            return {"issue": {"title": "Ship the ledger"}}

        monkeypatch.setattr(kb, "_kata", fake_show)
        event = {"type": "issue.links_changed", "actor": "patrick",
                 "issue_short_id": "abc4",
                 "payload": {"blocked_by_added": ["bm8a"]}}
        assert sentence(event) == 'patrick changed links on "Ship the ledger"'

    def test_link_adds_from_the_web_ui_are_delivered(self, monkeypatch):
        """The web UI's link form emits issue.linked, not issue.links_changed."""
        import claude_kitchen.kata_bridge as kb

        async def fake_show(workspace, *args):
            return {"issue": {"title": "Ship the ledger"}}

        monkeypatch.setattr(kb, "_kata", fake_show)
        event = {"type": "issue.linked", "actor": "patrick",
                 "issue_short_id": "abc4",
                 "payload": {"to_short_id": "qk0h", "type": "related"}}
        assert sentence(event) == 'patrick linked "Ship the ledger" to qk0h'

    def test_an_unknown_type_still_reaches_the_sous(self, monkeypatch):
        """kata may grow event types after this was written; none are dropped."""
        import claude_kitchen.kata_bridge as kb

        async def fake_show(workspace, *args):
            return {"issue": {"title": "Ship the ledger"}}

        monkeypatch.setattr(kb, "_kata", fake_show)
        event = {"type": "issue.something_new", "actor": "patrick",
                 "issue_short_id": "abc4", "payload": {"whatever": 1}}
        assert sentence(event) == 'patrick changed "Ship the ledger"'

    def test_project_events_have_no_title_to_quote(self):
        event = {"type": "project.metadata_updated", "actor": "patrick",
                 "payload": {"diff": {}}}
        assert sentence(event) == "patrick changed the project"


class TestTailFailureSurfaces:
    """Only a missing binary is a silent no-op. A tail that dies must take the
    block down with it, not leave a channel server holding the socket with kata
    delivery quietly dead."""

    def test_a_dying_tail_ends_the_block(self, monkeypatch):
        import claude_kitchen.kata_bridge as kb

        async def boom(base, push):
            raise RuntimeError("kata went away")

        monkeypatch.setattr(kb.shutil, "which", lambda _: "/usr/bin/kata")
        monkeypatch.setattr(kb, "_tail", boom)

        ran_to_completion = False

        async def main():
            nonlocal ran_to_completion
            async with kb.tailing(Path("/nonexistent"), None):
                await asyncio.sleep(5)
                ran_to_completion = True

        with pytest.raises(BaseExceptionGroup) as caught:
            asyncio.run(main())
        assert [str(e) for e in caught.value.exceptions] == ["kata went away"]
        assert not ran_to_completion

    def test_a_missing_binary_stays_a_silent_no_op(self, monkeypatch):
        import claude_kitchen.kata_bridge as kb

        monkeypatch.setattr(kb.shutil, "which", lambda _: None)

        async def main():
            async with kb.tailing(Path("/nonexistent"), None):
                return "block ran"

        assert asyncio.run(main()) == "block ran"
