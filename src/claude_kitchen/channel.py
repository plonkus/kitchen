"""Kitchen channel MCP server — bridges cook hooks to sous chef via Claude Code channels."""
import asyncio
import json
import socket as sock_mod
import sys
from pathlib import Path
from typing import get_args, get_origin

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

SOCK_NAME = "kitchen.sock"

# The capability that makes a sous hear anything. Claude reads it from a
# DIFFERENT place depending on the protocol era it negotiates with us:
# `experimental` on the handshake era (2025-11-25), `extensions` on the modern
# era (2026-07-28, SEP-2133). Declaring only one is not an error anywhere — the
# connection succeeds, the socket binds, cook reports arrive, and Claude
# silently drops every notification, logging "Channel notifications skipped:
# server did not declare claude/channel capability" where nobody looks. So
# declare it in both, and refuse to start if the SDK grows a third place.
CHANNEL_CAPABILITY = "claude/channel"


def _capability_maps(capabilities) -> list[str]:
    """Names of every free-form capability map the installed SDK exposes.

    Both `experimental` and `extensions` are `dict[str, dict[str, Any]]` on
    ServerCapabilities; the typed sub-capabilities (`tools`, `logging`, …) are
    models. An era that moves the capability again will land as another field
    of that same free-form shape, and this finds it without being told.

    Matched structurally, not by the annotation's text: this whole outage was a
    silent formatting assumption, and `str(annotation)` renders `Any` as
    `typing.Any` on some versions and not others."""
    def is_map(annotation) -> bool:
        for arg in get_args(annotation) or (annotation,):
            if get_origin(arg) is dict and get_args(arg)[0] is str:
                return get_origin(get_args(arg)[1]) is dict
        return False

    return [
        name for name, field in type(capabilities).model_fields.items()
        if is_map(field.annotation)
    ]


def _refuse_if_deaf(capabilities):
    """Exit rather than serve a connection that cannot deliver.

    A server that binds the socket but cannot push is worse than no server at
    all: it owns `kitchen.sock`, so cook hooks succeed and the sous hears
    nothing, and the replacement that would have worked stands down because the
    path is taken. Checked BEFORE the socket is claimed, so a deaf build dies
    without taking the kitchen with it."""
    missing = [
        name for name in _capability_maps(capabilities)
        if CHANNEL_CAPABILITY not in (getattr(capabilities, name) or {})
    ]
    if missing:
        sys.exit(
            f"kitchen channel-server: this mcp SDK carries capabilities in "
            f"{', '.join(missing)}, where we do not advertise "
            f"{CHANNEL_CAPABILITY}. A sous negotiating that protocol era would "
            f"connect, bind the socket and never hear a cook. Advertise "
            f"{CHANNEL_CAPABILITY} in {', '.join(missing)} in "
            f"create_initialization_options (channel.py)."
        )


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


def _claim_socket(sock_path: Path, _max_probes: int = 8):
    """Prepare sock_path for binding, refusing to stomp a live owner.

    Connect-probes an existing socket and branches on the EXACT outcome —
    "any connect failure == stale" is the bug we are fixing:
    - connect succeeds      → a live listener owns it → stand down (exit 0),
                              do NOT unlink/bind (idempotent /mcp reconnects).
    - ECONNREFUSED / ENOENT → genuinely stale/dead → unlink and let caller bind.
    - any other OSError     → unknown state (EACCES, ENOTSOCK, not-a-socket) →
                              do NOT unlink, fail loud (exit 1).

    TOCTOU guard: two servers probing the same STALE socket both see
    ECONNREFUSED; if both blindly unlink+bind, the second unlinks the first's
    now-LIVE socket and orphans it (last-writer-wins reopens). So we capture the
    file's inode identity BEFORE probing and only unlink if the path STILL refers
    to that exact inode. If it changed under us (someone rebound), we re-probe;
    the loop converges (the winner answers and we stand down).
    """
    for _ in range(_max_probes):
        try:
            before = sock_path.stat()
        except FileNotFoundError:
            return  # nothing there → cold start, let caller bind
        probe = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        try:
            probe.connect(str(sock_path))
        except (ConnectionRefusedError, FileNotFoundError):
            # Stale candidate — but only unlink if it's STILL the same inode we
            # just probed. If it changed, a peer rebound a live socket here; do
            # NOT unlink (that would orphan it) → re-probe.
            try:
                now = sock_path.stat()
            except FileNotFoundError:
                return  # peer cleared it for us → bind
            if (now.st_ino, now.st_dev) != (before.st_ino, before.st_dev):
                continue
            sock_path.unlink(missing_ok=True)
            return
        except OSError as e:
            print(
                f"kitchen channel-server: refusing to bind {sock_path}: unexpected "
                f"error probing existing socket ({e!r}); leaving it untouched.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        else:
            # We don't name the owner pid: peer-pid lookup over AF_UNIX is not
            # portable (Linux SO_PEERCRED vs macOS LOCAL_PEERPID), and §Design.2
            # explicitly says not to rely on it — the path identifies the socket.
            print(
                f"kitchen channel-server: {sock_path} already has a live owner; "
                f"standing down.",
                file=sys.stderr,
            )
            raise SystemExit(0)
        finally:
            probe.close()
    # The path kept getting rebound under us across every probe — rather than
    # risk orphaning whoever currently owns it, stand down cleanly.
    print(
        f"kitchen channel-server: {sock_path} kept changing under probe; "
        f"standing down.",
        file=sys.stderr,
    )
    raise SystemExit(0)


async def run_server(kitchen: str):
    """Run the MCP channel server with a unix socket listener."""
    from claude_kitchen.state import state_dir

    base = state_dir(kitchen)
    base.mkdir(parents=True, exist_ok=True)
    sock_path = base / SOCK_NAME

    server = Server("kitchen")
    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={CHANNEL_CAPABILITY: {}},
    )
    # Before the socket is claimed: a server that cannot deliver must not own
    # the path the next one needs.
    _refuse_if_deaf(init_options.capabilities)
    _claim_socket(sock_path)

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
            method=f"notifications/{CHANNEL_CAPABILITY}",
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
    """Send a JSON line to the kitchen socket.

    Loud, not fatal: a wedged/hijacked/dead socket is surfaced to stderr (path +
    errno) so the failure is immediately diagnosable, but never crashes the
    calling cook hook — a dead channel must not break the cook.
    """
    try:
        s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        s.connect(str(sock_path))
        s.sendall(json.dumps(data).encode() + b"\n")
        s.shutdown(sock_mod.SHUT_WR)
        s.close()
    except OSError as e:
        # ConnectionError / FileNotFoundError are OSError subclasses.
        print(
            f"kitchen channel: failed to notify sous via {sock_path}: "
            f"[errno {e.errno}] {e.strerror or e}",
            file=sys.stderr,
        )
