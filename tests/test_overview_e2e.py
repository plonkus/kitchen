"""End-to-end overview v2 flow, fully mocked — no real Claude / uvicorn / tmux.

Walks the whole structured pipeline in one test: `kitchen open` auto-starts the
overview → a real `_overview_loop_tick` gates on changed kitchens, runs the
one-shot (mocked `claude -p`), validates → wraps → writes `synopsis.json`, and
broadcasts `loop_tick` over the WS → a connected dashboard re-fetches /state and
sees the structured contract with time-independent grouping → an idle tick
spawns zero `claude` → a sous flipping to "working" clears out of Waiting →
`kitchen close overview` tears the server down while synopsis files survive.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from claude_kitchen import cli
from claude_kitchen.dashboard_server import app

client = TestClient(app)

# What the one-shot overview-sous emits to stdout: exactly the four judgment
# fields, bare JSON (the loop adds the envelope).
ONE_SHOT = {
    "line": "Holding the migration fix; rollout gated on the head chef.",
    "block": "Approve the rollout window for the migration fix",
    "actions": ["Reply go/no-go in the plow kitchen", "Approve the deploy"],
    "urgency": "high",
}


def test_overview_v2_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".claude-kitchen"

    # A real kitchen, blocked-and-OLD: its sous went idle 2 days ago. The §3
    # time-independence rule says it must still surface as waiting_on_you (and
    # never dormant) once its synopsis carries a block.
    kdir = root / "plow-main"
    kdir.mkdir(parents=True)
    (kdir / "kitchen.json").write_text(json.dumps({"source": "/p/plow"}))
    (kdir / "sous.json").write_text(json.dumps(
        {"status": "idle", "ts": "2026-06-07T09:00:00Z", "sous_session_id": "sid-1"}))
    two_days_ago = time.time() - 2 * 86400
    os.utime(kdir / "sous.json", (two_days_ago, two_days_ago))

    # --- Phase 1: `kitchen open <foo>` auto-starts the overview --------------
    open_target = root / "open-target"  # cmd_open's own state dir (kept separate)
    args = MagicMock()
    args.name = None              # use project.name, no worktree branch
    args.project = "/p/plow"
    args.worktree_path = None
    args.resume = False
    with patch("claude_kitchen.cli._ensure_overview_running") as ensure, \
         patch("claude_kitchen.cli.resolve_project", return_value=Path("/p/plow")), \
         patch("claude_kitchen.cli.project_slug", return_value="plow"), \
         patch("claude_kitchen.cli.namespaced", return_value="plow-open"), \
         patch("claude_kitchen.cli.state_dir", return_value=open_target), \
         patch("claude_kitchen.cli.has_session", return_value=False), \
         patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)), \
         patch("claude_kitchen.cli.spawn_sous"):
        cli.cmd_open(args)
    ensure.assert_called_once()  # the dashboard is ensured up at the start of open

    # --- Phase 2: a real loop tick — gate → one-shot → validate/wrap/write ---
    # `claude -p` is the only mock: it returns the bare four-field JSON the
    # role emits. The broadcast POSTs the real /internal/loop-tick via the
    # TestClient, so the WS push is exercised for real too.
    claude = MagicMock(return_value=MagicMock(
        returncode=0, stdout=json.dumps(ONE_SHOT)))
    tick_post = MagicMock(side_effect=lambda: client.post("/internal/loop-tick"))
    with patch("claude_kitchen.cli.subprocess.run", claude), \
         patch("claude_kitchen.cli._broadcast_loop_tick", tick_post), \
         client.websocket_connect("/events") as ws:
        assert cli._overview_loop_tick("opus") == 1
        msg = ws.receive_json()    # the dashboard's WS wake-up
        assert msg["type"] == "loop_tick" and msg["ts"].endswith("Z")

        # The changed kitchen got exactly ONE fresh one-shot, with an explicit
        # model and the role as system prompt; bounded inputs ride the stdin.
        assert claude.call_count == 1
        argv = claude.call_args.args[0]
        assert argv[:2] == ["claude", "-p"]
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--append-system-prompt-file") + 1].endswith(
            "overview-sous.md")
        assert "plow-main" in claude.call_args.kwargs["input"]

        # An idle tick (nothing changed since the write) spawns ZERO claude
        # and broadcasts nothing — the idle path costs no tokens.
        assert cli._overview_loop_tick("opus") == 0
        assert claude.call_count == 1
        assert tick_post.call_count == 1

    # The loop wrapped the four fields with the envelope and wrote the full
    # 7-field synopsis.json.
    syn = json.loads((kdir / "synopsis.json").read_text())
    assert syn["kitchen"] == "plow-main"
    assert syn["based_on_mtime"] == "2026-06-07T09:00:00Z"  # sous.json's ts
    datetime.strptime(syn["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    for field, value in ONE_SHOT.items():
        assert syn[field] == value

    # --- Phase 3: /state serves the structured contract ----------------------
    state = client.get("/state").json()
    by = {k["name"]: k for k in state["kitchens"]}
    k = by["plow-main"]
    assert "synopsis" not in k  # the old prose field is gone
    assert k["line"] == ONE_SHOT["line"]
    assert k["block"] == ONE_SHOT["block"]
    assert k["actions"] == ONE_SHOT["actions"]
    assert k["urgency"] == "high"
    assert k["synopsis_generated_at"] == syn["generated_at"]
    # Time-independent grouping: blocked 2 days → STILL waiting_on_you, and the
    # dormant flag may not re-hide it.
    assert k["status"] == "waiting_on_you"
    assert k["dormant"] is False

    # --- Phase 4: regression — sous flips to "working" → clears Waiting ------
    (kdir / "sous.json").write_text(json.dumps(
        {"status": "working", "ts": "2026-06-09T10:00:00Z",
         "sous_session_id": "sid-1"}))  # fresh mtime: the sous picked work up
    after = {k["name"]: k for k in client.get("/state").json()["kitchens"]}
    assert after["plow-main"]["status"] == "working"  # out of waiting_on_you
    assert after["plow-main"]["dormant"] is False

    # --- Phase 5: `kitchen close overview` tears down, synopses survive ------
    (root / "overview").mkdir(parents=True)
    (root / "overview" / "kitchen.json").write_text("{}")
    with patch("claude_kitchen.cli.resolve_kitchen", return_value="overview"), \
         patch("claude_kitchen.cli._terminate_overview_server") as term, \
         patch("claude_kitchen.cli.tmux") as ktmux:
        cli.cmd_close(MagicMock())
    term.assert_called_once()  # graceful (SIGTERM) server shutdown fired
    assert any(c.args[:1] == ("kill-session",) for c in ktmux.call_args_list)
    assert not (root / "overview" / "kitchen.json").exists()  # overview torn down
    assert (kdir / "synopsis.json").exists()                  # synopsis preserved
    assert json.loads((kdir / "synopsis.json").read_text()) == syn
