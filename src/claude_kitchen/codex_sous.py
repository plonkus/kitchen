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
            else:
                self.notes.append(msg)

    async def wait_note(self, method: str, timeout: float = 300):
        """Block until a notification with this method arrives; return its params."""
        seen = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            while seen < len(self.notes):
                note = self.notes[seen]
                seen += 1
                if note.get("method") == method:
                    return note.get("params", {})
            await asyncio.sleep(0.05)
        raise TimeoutError(f"no {method} notification within {timeout}s")

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
            await asyncio.sleep(0.02)
        raise TimeoutError(f"{method} timed out after {timeout}s")

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


def start_app_server(kitchen: str, base: Path, slug: str = None,
                     timeout: int = 60) -> int:
    """Launch the app-server in its own tmux window and return its port.

    The kitchen env goes HERE, not on the TUI. A claude sous runs its own tool
    calls, so exporting AGENT_KITCHEN around it is enough; a codex sous does
    not — the TUI is a viewer and every shell command the sous runs is executed
    by the app-server process. Set it on the TUI instead and the sous's first
    `kitchen ticket` answers "No active kitchens."

    Readiness is a successful `initialize`, not a TCP connect: the listener is
    up well before the server will answer, and cmd_open's very next move is
    thread/start.
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

    async def wait():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                c = await Client.connect(port)
            except (OSError, websockets.exceptions.WebSocketException):
                await asyncio.sleep(0.3)
                continue
            await c.close()
            return True
        return False

    if not asyncio.run(wait()):
        sys.exit(f"codex app-server never answered on port {port} (window "
                 f"{APP_SERVER_WINDOW} has the log)")
    return port


def create_sous_thread(port: int, sous_prompt: str, cwd: Path) -> str:
    """Create the sous's thread and seed it with one throwaway turn.

    The seed turn is load-bearing, not a smoke test: a thread with zero turns
    has no rollout file on disk, and `codex … resume <id>` fails with "no
    rollout found for thread id". So the thread must speak once before the TUI
    can attach to it.
    """
    async def go():
        c = await Client.connect(port)
        thread = (await c.call("thread/start", {
            "cwd": str(cwd),
            "developerInstructions": sous_prompt,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }))["thread"]
        await c.call("turn/start", {
            "threadId": thread["id"],
            "input": [{"type": "text",
                       "text": "You are booting. Reply with one short line "
                               "(\"Ready, chef.\") and wait.",
                       "text_elements": []}],
        })
        await c.wait_note("turn/completed")
        await c.close()
        return thread["id"]

    return asyncio.run(go())


class Sous:
    """The bridge's live handle on the sous's thread.

    One long-lived connection, not a fresh one per report, because knowing
    whether the sous is mid-turn requires being SUBSCRIBED: a connection that
    merely opened the socket sees nothing (verified — it receives only
    thread/status/changed), while one that has called thread/resume receives
    turn/started and turn/completed for turns any other client — including the
    human's TUI — starts. Asking the thread instead of listening is not an
    option: thread/read reconstructs turns from the rollout on disk, which lags
    the turn in progress.
    """

    def __init__(self, client: Client, thread_id: str):
        self.c = client
        self.thread_id = thread_id
        self._seen = 0
        self._turn = None

    @classmethod
    async def attach(cls, port: int, thread_id: str):
        c = await Client.connect(port)
        # Subscribes this connection to the thread's notifications. The TUI
        # rejoins the same thread the same way; they coexist.
        await c.call("thread/resume", {"threadId": thread_id})
        return cls(c, thread_id)

    def in_flight_turn(self):
        """Id of the turn the sous is running right now, or None if it's idle."""
        while self._seen < len(self.c.notes):
            note = self.c.notes[self._seen]
            self._seen += 1
            if note.get("method") == "turn/started":
                self._turn = note["params"]["turn"]["id"]
            elif note.get("method") == "turn/completed":
                self._turn = None
        return self._turn

    async def push(self, text: str):
        """Deliver one cook report into the sous's thread.

        Idle sous → turn/start, which is the whole point: the report arrives as
        a turn and the sous acts on it, unprompted. Sous mid-turn → turn/steer,
        which folds the report into the turn already running. turn/start would
        also work there (it queues), but a sous mid-thought about cook A should
        hear about cook B now, not after it finishes.
        """
        item = [{"type": "text", "text": text, "text_elements": []}]
        turn_id = self.in_flight_turn()
        if turn_id:
            await self.c.call("turn/steer", {"threadId": self.thread_id,
                                             "expectedTurnId": turn_id,
                                             "input": item})
        else:
            await self.c.call("turn/start", {"threadId": self.thread_id,
                                             "input": item})


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
    sous = await Sous.attach(port, thread_id)

    async def notify(data: dict):
        await sous.push(_format(data))

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
