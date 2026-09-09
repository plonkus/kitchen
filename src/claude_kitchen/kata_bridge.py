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

# One plain sentence per event type, in the head chef's language. A type absent
# here is not something the sous acts on and is dropped.
SENTENCES = {
    "issue.created": '{actor} filed "{title}"',
    "issue.commented": '{actor} commented on "{title}": {body}',
    "issue.closed": '{actor} closed "{title}" as {reason}: {message}',
    "issue.updated": '{actor} retitled "{old_title}" to "{title}"',
    "issue.assigned": '{actor} put {owner} on "{title}"',
    "issue.unassigned": '{actor} took {owner} off "{title}"',
    "issue.labeled": '{actor} labelled "{title}" {label}',
    "issue.unlabeled": '{actor} took the {label} label off "{title}"',
    "issue.metadata_updated": '{actor} changed metadata on "{title}"',
}

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


async def _sentence(workspace: Path, event: dict) -> str | None:
    template = SENTENCES.get(event["type"])
    if template is None or event["actor"] == SOUS_ACTOR:
        return None
    fields = {k: _prose(str(v)) for k, v in event["payload"].items()}
    # Only `issue.created` and `issue.updated` carry the title in the payload;
    # for the rest the issue has to be asked.
    if "title" not in fields:
        shown = await _kata(workspace, "show", event["issue_short_id"])
        fields["title"] = shown["issue"]["title"]
    return template.format(actor=event["actor"], **fields)


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
                # `source` is the channel server's own name and Claude Code
                # emits it itself; a meta key of that name lands as a second,
                # duplicate attribute. `kata` is what marks the tag as a ledger
                # event, and carries the ref the sous needs to act on it.
                await push(sentence, {"kata": event["issue_short_id"]})
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
    """Push kata events into `push` for as long as the block runs."""
    if shutil.which("kata") is None:
        yield
        return
    task = asyncio.create_task(_tail(base, push))
    try:
        yield
    finally:
        task.cancel()
