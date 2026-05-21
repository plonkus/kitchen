"""Kitchen channel MCP server — bridges cook hooks to sous chef via Claude Code channels."""
import asyncio
import json
import socket as sock_mod
from pathlib import Path

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

SOCK_NAME = "kitchen.sock"


async def handle_connection(reader: asyncio.StreamReader, writer, notify):
    """Handle one cook hook connection: read JSON line, call notify."""
    try:
        line = await reader.readline()
        if not line:
            return
        data = json.loads(line)
        await notify(data)
    except (json.JSONDecodeError, ConnectionError):
        pass
    finally:
        if writer:
            writer.close()


async def run_server(kitchen: str):
    """Run the MCP channel server with a unix socket listener."""
    from claude_kitchen.state import state_dir

    base = state_dir(kitchen)
    base.mkdir(parents=True, exist_ok=True)
    sock_path = base / SOCK_NAME
    sock_path.unlink(missing_ok=True)

    server = Server("kitchen")
    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={"claude/channel": {}},
    )

    # Mutable container so notify closure can access write_stream
    # after stdio_server connects.
    state = {"write_stream": None}

    async def notify(data: dict):
        ws = state["write_stream"]
        if ws is None:
            return
        cook = data.get("cook", "unknown")
        summary = data.get("summary", "")
        ts = data.get("ts", "")
        meta = {"cook": cook, "ts": ts}
        # ctx is omitted (not passed null/empty) when the cook has no
        # token info yet — the sous shouldn't see ctx="" or ctx="null".
        if data.get("ctx"):
            meta["ctx"] = data["ctx"]
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": summary, "meta": meta},
        )
        await ws.send(SessionMessage(message=JSONRPCMessage(notification)))

    async def on_connection(reader, writer):
        await handle_connection(reader, writer, notify)

    # Start socket server FIRST so it's ready before MCP handshake
    sock_server = await asyncio.start_unix_server(
        on_connection, path=str(sock_path)
    )

    try:
        async with stdio_server() as (read_stream, write_stream):
            state["write_stream"] = write_stream
            await server.run(read_stream, write_stream, init_options)
    finally:
        sock_server.close()
        await sock_server.wait_closed()
        sock_path.unlink(missing_ok=True)


def main(kitchen: str):
    """Entry point for the channel server process."""
    asyncio.run(run_server(kitchen))


def send_to_socket(sock_path: Path, data: dict):
    """Send a JSON line to the kitchen socket. Fails silently if unavailable."""
    try:
        s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        s.connect(str(sock_path))
        s.sendall(json.dumps(data).encode() + b"\n")
        s.shutdown(sock_mod.SHUT_WR)
        s.close()
    except (ConnectionError, FileNotFoundError, OSError):
        pass
