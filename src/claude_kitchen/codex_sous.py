"""Codex sous chef: app-server plumbing and the cook→sous push bridge.

A claude sous RECEIVES because Claude Code has channels: the kitchen MCP server
pushes `notifications/claude/channel` into a running session (see channel.py).
Codex has no such MCP extension, but it has something better — the app-server.
`codex app-server --listen ws://…` runs the agent loop out of process; a TUI
attaches with `codex --remote <addr> resume <thread>`, and ANY other client on
that socket can inject into the same thread while the human watches. That is the
whole basis for a codex sous:

    kitchen open --backend codex
      ├─ tmux window `_appserver`: codex app-server --listen ws://127.0.0.1:PORT
      ├─ thread/start (developerInstructions = sous-chef.md, cwd = project)
      ├─ tmux window `_bridge`:    kitchen codex-channel <kitchen>
      └─ execvp codex --remote ws://127.0.0.1:PORT resume <thread>

The bridge owns the same `kitchen.sock` a claude kitchen's channel-server owns,
speaks the same JSON-line protocol to the same cook hooks, and turns each line
into a turn on the sous's thread.
"""
import asyncio
import json
import shlex
import socket
import sys
import time
from pathlib import Path

import websockets

from claude_kitchen.channel import SOCK_NAME, _claim_socket, handle_connection
from claude_kitchen.state import notes_dir, state_dir, wiki_dir
from claude_kitchen.tmux import tmux, target

APP_SERVER_WINDOW = "_appserver"
BRIDGE_WINDOW = "_bridge"

# Both windows are `_`-prefixed so `kitchen brigade` hides them, and both live in
# the kitchen's own tmux session so `kitchen close` kills them with the session.

# The only notifications anything here waits on. A subscribed connection also
# streams reasoning/message/tool-output deltas by the hundred per turn; keeping
# them would grow a long-lived client's buffer without bound for no reader.
_LIFECYCLE = ("turn/started", "turn/completed")


class Client:
    """One websocket JSON-RPC session against the app-server.

    Codex's app-server takes MCP-shaped JSON-RPC over a websocket. Every
    connection must `initialize` + `initialized` before any other method, so
    `connect` does both and hands back a client that can only call.
    """

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._resp = {}
        self.notes = []
        self._rx = asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and ("result" in msg or "error" in msg):
                self._resp[msg["id"]] = msg
            elif msg.get("method") in _LIFECYCLE:
                self.notes.append(msg)

    def _check_alive(self):
        """Surface a dead reader NOW rather than after a caller's timeout.

        Without this, a closed socket looks exactly like a slow server: the
        response never arrives and every call blocks for its full timeout —
        two silent minutes per cook report, in a tmux window nobody is watching.
        """
        if self._rx.done():
            self._rx.result()  # re-raises whatever killed the reader
            raise ConnectionError("app-server closed the connection")

    async def call(self, method: str, params: dict = None, timeout: float = 120):
        self._id += 1
        rid = self._id
        await self.ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        ))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self._resp:
                msg = self._resp.pop(rid)
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg["result"]
            self._check_alive()
            await asyncio.sleep(0.02)
        raise TimeoutError(f"{method} timed out after {timeout}s")

    async def wait_note(self, method: str, turn_id: str = None,
                        timeout: float = 300):
        """Block until a matching notification arrives; return its params.

        `turn_id` matters: the sous's thread can have more than one turn in
        flight, so "some turn finished" is not "MY turn finished".
        """
        seen = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            while seen < len(self.notes):
                note = self.notes[seen]
                seen += 1
                params = note.get("params", {})
                if note.get("method") != method:
                    continue
                if turn_id and params.get("turn", {}).get("id") != turn_id:
                    continue
                return params
            self._check_alive()
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no {method} notification within {timeout}s")

    @classmethod
    async def connect(cls, port: int):
        ws = await websockets.connect(f"ws://127.0.0.1:{port}", max_size=None)
        self = cls(ws)
        await self.call("initialize", {
            "clientInfo": {"name": "claude-kitchen", "title": None, "version": "0.1.0"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        })
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))
        return self

    async def close(self):
        self._rx.cancel()
        await self.ws.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _app_server_window(kitchen: str, base: Path, slug: str = None) -> int:
    """Launch the app-server in its own tmux window. Returns its port.

    The kitchen env goes HERE, not on the TUI. A claude sous runs its own tool
    calls, so exporting AGENT_KITCHEN around it is enough; a codex sous does
    not — the TUI is a viewer and every shell command the sous runs is executed
    by the app-server process. Set it on the TUI instead and the sous's first
    `kitchen ticket` answers "No active kitchens."
    """
    port = _free_port()
    q = shlex.quote
    env = {"AGENT_KITCHEN": kitchen, "AGENT_NAME": "sous", "STATUS_DIR": str(base)}
    if slug:
        env["KITCHEN_WIKI"] = str(wiki_dir(slug))
        env["KITCHEN_NOTES"] = str(notes_dir(kitchen))
    exports = " ".join(f"{k}={q(v)}" for k, v in env.items())
    tmux("kill-window", "-t", target(APP_SERVER_WINDOW), kitchen=kitchen)
    r = tmux("new-window", "-d", "-t", target(), "-n", APP_SERVER_WINDOW,
             f"export {exports}; exec codex app-server --listen ws://127.0.0.1:{port}",
             kitchen=kitchen)
    if r.returncode != 0:
        sys.exit(f"could not launch the codex app-server window on port {port}: "
                 f"{r.stderr.strip()}")
    return port


async def _connect_when_ready(port: int, timeout: int = 60) -> Client:
    """Readiness is a successful `initialize`, not a TCP connect: the listener
    is up well before the server will answer."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return await Client.connect(port)
        except (OSError, websockets.exceptions.WebSocketException):
            await asyncio.sleep(0.3)
    raise TimeoutError(f"codex app-server never answered on port {port}")


def bootstrap(kitchen: str, base: Path, sous_prompt: str, project: Path,
              slug: str = None) -> tuple[int, str]:
    """Stand the sous's app-server and thread up. Returns (port, thread_id).

    One owner for the whole startup — one window, one event loop, one
    connection — so that a failure anywhere in it tears the app-server window
    back down instead of leaving an orphan behind a half-open kitchen.

    The seed turn is load-bearing, not a smoke test: a thread with zero turns
    has no rollout file on disk, and `codex … resume <id>` fails with "no
    rollout found for thread id". So the thread must speak once before the TUI
    can attach to it — and it must speak SUCCESSFULLY, or the sous the head
    chef is about to be handed is already broken.
    """
    port = _app_server_window(kitchen, base, slug)

    async def go():
        c = await _connect_when_ready(port)
        thread = (await c.call("thread/start", {
            "cwd": str(project),
            "developerInstructions": sous_prompt,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }))["thread"]
        turn = (await c.call("turn/start", {
            "threadId": thread["id"],
            "input": [{"type": "text",
                       "text": "You are booting. Reply with one short line "
                               "(\"Ready, chef.\") and wait.",
                       "text_elements": []}],
        }))["turn"]
        done = await c.wait_note("turn/completed", turn_id=turn["id"])
        status = done["turn"]["status"]
        if status != "completed":
            raise RuntimeError(
                f"the sous's first turn ended {status}: {done['turn'].get('error')}"
            )
        await c.close()
        return thread["id"]

    try:
        return port, asyncio.run(go())
    except Exception as e:
        tmux("kill-window", "-t", target(APP_SERVER_WINDOW), kitchen=kitchen)
        sys.exit(f"codex sous failed to start: {e}")


async def push(port: int, thread_id: str, text: str):
    """Deliver one cook report into the sous's thread.

    Always `turn/start`, never `turn/steer`. An idle sous starts a turn on the
    report — the whole point. A BUSY sous queues it and runs it when the current
    turn ends (verified against app-server 0.147.0: turn/start on a thread with
    a turn in flight returns a new turn in `inProgress` and it runs afterwards).
    Steering the in-flight turn instead would deliver a report a few seconds
    sooner and cost a race — the expected turn can complete between reading its
    id and sending the steer, and app-server rejects a steer whose expectedTurnId
    is no longer running, which drops the report on the floor.

    A connection per report, rather than one held open for the kitchen's life:
    nothing here needs to be subscribed any more, and a socket that is opened
    when it is used cannot go quietly stale between cooks.
    """
    c = await Client.connect(port)
    try:
        await c.call("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        })
    finally:
        await c.close()


def _format(data: dict) -> str:
    """Render a cook hook's JSON line as the sous sees it.

    Deliberately the shape of Claude's `<channel>` tag: sous-chef.md is written
    against that vocabulary and is shared verbatim by both backends.
    """
    attrs = f'cook="{data.get("cook", "unknown")}" ts="{data.get("ts", "")}"'
    if data.get("ctx"):
        attrs += f' ctx="{data["ctx"]}"'
    return f"<channel {attrs}>\n{data.get('summary', '')}\n</channel>"


async def run_bridge(kitchen: str):
    base = state_dir(kitchen)
    kj = json.loads((base / "kitchen.json").read_text())
    port, thread_id = kj["codex_ws_port"], kj["codex_thread_id"]
    sock_path = base / SOCK_NAME
    _claim_socket(sock_path)

    async def notify(data: dict):
        await push(port, thread_id, _format(data))

    server = await asyncio.start_unix_server(
        lambda r, w: handle_connection(r, w, notify), path=str(sock_path)
    )
    print(f"kitchen codex-channel: {sock_path} → thread {thread_id} on port {port}",
          file=sys.stderr)
    try:
        async with server:
            await server.serve_forever()
    finally:
        sock_path.unlink(missing_ok=True)


def bridge_main(kitchen: str):
    asyncio.run(run_bridge(kitchen))


def start_bridge(kitchen: str):
    """Run the bridge in its own tmux window.

    sys.argv[0], not a bare `kitchen`: the bridge must be the same build that
    opened the kitchen, and in a worktree/venv checkout the two differ.
    """
    tmux("kill-window", "-t", target(BRIDGE_WINDOW), kitchen=kitchen)
    if tmux("new-window", "-d", "-t", target(), "-n", BRIDGE_WINDOW,
            f"exec {sys.argv[0]} codex-channel {kitchen}",
            kitchen=kitchen).returncode != 0:
        sys.exit("could not launch the codex-channel bridge window")
