"""End-to-end overview v2 flow, fully mocked — no real Claude / uvicorn / tmux.

Walks the whole pipeline in one test: `kitchen open` auto-starts the overview →
the loop summarizer writes a synopsis → a loop_tick is broadcast over the WS →
a connected dashboard re-fetches /state and sees the fresh synopsis →
`kitchen close overview` tears the server down while synopsis files survive.
"""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from claude_kitchen import cli
from claude_kitchen.dashboard_server import app

client = TestClient(app)


def test_overview_v2_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".claude-kitchen"

    # A real kitchen with recent sous activity but no synopsis yet.
    kdir = root / "plow-main"
    kdir.mkdir(parents=True)
    (kdir / "kitchen.json").write_text(json.dumps({"source": "/p/plow"}))
    (kdir / "sous.json").write_text(json.dumps(
        {"status": "idle", "ts": "2026-06-03T18:43:12Z", "sous_session_id": "sid-1"}))

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

    # --- Phase 2: loop tick — the overview sous writes a synopsis ------------
    # (the sous's LLM output; we write the file it would produce, per the spec's
    # frontmatter shape: generated_at + based_on_mtime + kitchen.)
    body = "Cracked the migration bug; holding the fix and waiting on the rollout call."
    (kdir / "synopsis.md").write_text(
        "---\n"
        "generated_at: 2026-06-03T18:45:00Z\n"
        "based_on_mtime: 2026-06-03T18:43:12Z\n"
        "kitchen: plow-main\n"
        "---\n"
        f"{body}\n"
    )

    # --- Phase 3 + 4: broadcast loop_tick → dashboard re-fetches /state ------
    with client.websocket_connect("/events") as ws:
        assert client.post("/internal/loop-tick").status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "loop_tick" and msg["ts"].endswith("Z")
        state = client.get("/state").json()  # the dashboard's re-fetch on loop_tick

    by = {k["name"]: k for k in state["kitchens"]}
    assert "plow-main" in by
    assert by["plow-main"]["synopsis"] == body
    assert by["plow-main"]["synopsis_generated_at"] == "2026-06-03T18:45:00Z"

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
    assert (kdir / "synopsis.md").exists()                    # synopsis preserved
    assert body in (kdir / "synopsis.md").read_text()
