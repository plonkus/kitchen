"""kata event bridge — the ledger's live half.

The head chef edits issues in the kata web UI; those edits reach the sous here,
as channel messages, so the sous never polls and chat never drifts from the
ledger. Experimental, and deliberately rip-out-able: this file plus the one
`async with tailing(...)` in `channel.run_server`. If `kata` is not on PATH
there is no ledger on this machine and the bridge never starts; anything else
that goes wrong is a setup error and raises.
"""
import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

# Where the last delivered event id lives, so a channel-server restart resumes
# instead of replaying the project's history into the sous.
CURSOR_NAME = "kata-cursor"

# The sous is kata's only writer and `kitchen open` gives it KATA_AUTHOR=sous,
# so its own writes come straight back down the tail. Pushing them would echo.
SOUS_ACTOR = "sous"

# One plain sentence per event key, in the head chef's language.
SENTENCES = {
    "issue.created": '{actor} filed "{title}"',
    "issue.commented": '{actor} commented on "{title}": {body}',
    "issue.closed": '{actor} closed "{title}" as {reason}: {message}',
    "issue.updated.title": '{actor} renamed "{old_title}" to "{title}"',
    "issue.updated.body": '{actor} edited the description of "{title}"',
    "issue.updated.other": '{actor} updated "{title}"',
    "issue.assigned": '{actor} put {owner} on "{title}"',
    "issue.unassigned": '{actor} took {owner} off "{title}"',
    "issue.labeled": '{actor} labelled "{title}" {label}',
    "issue.unlabeled": '{actor} took the {label} label off "{title}"',
    "issue.metadata_updated": '{actor} changed metadata on "{title}"',
    "issue.priority_set": '{actor} set priority {priority} on "{title}"',
    "issue.links_changed": '{actor} changed links on "{title}"',
    # The web UI's own link form emits this rather than links_changed.
    "issue.linked": '{actor} linked "{title}" to {to_short_id}',
}

# Every event reaches the sous, including types kata grows after this was
# written: an unnamed one still says who touched what, and the sous reads the
# issue when that is not enough.
DEFAULT_SENTENCE = '{actor} changed "{title}"'

# A handful of events are about the project rather than an issue, so there is
# no title to quote.
PROJECT_SENTENCE = "{actor} changed the project"

# Comment and close messages are written for the web UI and run long; the sous
# reads the issue itself when it needs the rest.
PROSE_LIMIT = 400


async def _kata(workspace: Path, *args) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "kata", *args, "--json", "--workspace", str(workspace),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return json.loads(out)


def _prose(text: str) -> str:
    """One line, short enough to read in a channel tag."""
    line = " ".join(text.split())
    return line[:PROSE_LIMIT - 1] + "…" if len(line) > PROSE_LIMIT else line


def _key(event: dict) -> str:
    """Which sentence an event reads under. `issue.updated` covers title edits,
    description edits and both at once, and only carries the fields the edit
    actually touched, so it splits by what is there."""
    if event["type"] != "issue.updated":
        return event["type"]
    for field in ("title", "body"):
        if field in event["payload"]:
            return f"issue.updated.{field}"
    return "issue.updated.other"


async def _sentence(workspace: Path, event: dict) -> str | None:
    if event["actor"] == SOUS_ACTOR:
        return None
    if "issue_short_id" not in event:
        return PROJECT_SENTENCE.format(actor=event["actor"])
    fields = {k: _prose(str(v)) for k, v in event["payload"].items()}
    # Only issue.created and a title edit carry the title in the payload; for
    # the rest the issue has to be asked.
    if "title" not in fields:
        shown = await _kata(workspace, "show", event["issue_short_id"])
        fields["title"] = shown["issue"]["title"]
    template = SENTENCES.get(_key(event), DEFAULT_SENTENCE)
    return template.format(actor=event["actor"], **fields)


def _meta(event: dict) -> dict:
    """Channel tag attributes. `source` is the channel server's own name and
    Claude Code emits it itself; a meta key of that name would land as a second,
    duplicate attribute. `kata` is what marks the tag as a ledger event and
    carries the ref the sous acts on — a project event has no issue behind it,
    so it carries nothing."""
    if "issue_short_id" not in event:
        return {}
    return {"kata": event["issue_short_id"]}


async def _tail(base: Path, push):
    workspace = Path(_workspace(base))
    cursor = base / CURSOR_NAME
    if not cursor.exists():
        # Cold start: begin at the tip. The history is the head chef's to read
        # in the web UI, not a backlog to replay into a fresh sous.
        events = (await _kata(workspace, "events", "--limit", "100000"))["events"]
        cursor.write_text(str(events[-1]["event_id"]) if events else "0")

    proc = await asyncio.create_subprocess_exec(
        "kata", "events", "--tail", "--json", "--workspace", str(workspace),
        "--last-event-id", cursor.read_text().strip(),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async for line in proc.stdout:
            event = json.loads(line)
            sentence = await _sentence(workspace, event)
            if sentence:
                await push(sentence, _meta(event))
            cursor.write_text(str(event["event_id"]))
    finally:
        proc.terminate()


def _workspace(base: Path) -> str:
    """The sous's checkout — a worktree kitchen resolves the same kata project
    as its source repo, but only the path it actually works in exists on disk."""
    kitchen = json.loads((base / "kitchen.json").read_text())
    return kitchen.get("worktree", kitchen["source"])


@asynccontextmanager
async def tailing(base: Path, push):
    """Push kata events into `push` for as long as the block runs.

    A tail that dies takes the block down with it. A channel server that keeps
    the socket while kata delivery is quietly dead is the same failure
    `_refuse_if_deaf` exists to prevent: the sous believes it is being told
    about the ledger and is not, and the restart that would have fixed it never
    happens because nothing looks wrong."""
    if shutil.which("kata") is None:
        yield
        return
    async with asyncio.TaskGroup() as group:
        tail = group.create_task(_tail(base, push))
        try:
            yield
        finally:
            tail.cancel()
