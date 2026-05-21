"""Tests for the kitchen channel MCP server."""
import asyncio
import json
import socket as sock_mod
import tempfile
import threading
import time
from pathlib import Path

from claude_kitchen.channel import handle_connection, send_to_socket


class TestHandleConnection:
    """Test the socket connection handler."""

    def test_parses_json_line(self):
        data = {"cook": "eng", "summary": "tests pass", "ts": "2026-03-26T10:00:00Z"}

        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps(data).encode() + b"\n")
            reader.feed_eof()
            notified = []
            await handle_connection(reader, None, lambda d: _append(notified, d))
            return notified

        async def _append(lst, d):
            lst.append(d)

        notified = asyncio.run(run())
        assert notified == [data]

    def test_ignores_invalid_json(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"not json\n")
            reader.feed_eof()
            notified = []
            await handle_connection(reader, None, lambda d: _append(notified, d))
            return notified

        async def _append(lst, d):
            lst.append(d)

        notified = asyncio.run(run())
        assert notified == []

    def test_ignores_empty_connection(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_eof()
            notified = []
            await handle_connection(reader, None, lambda d: _append(notified, d))
            return notified

        async def _append(lst, d):
            lst.append(d)

        notified = asyncio.run(run())
        assert notified == []


class TestSendToSocket:
    """Test the hook-side socket client."""

    def test_sends_json_to_socket(self):
        # Use /tmp to avoid AF_UNIX path length limits
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            sock_path = Path(td) / "k.sock"
            received = []

            def run_server():
                srv = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
                srv.bind(str(sock_path))
                srv.listen(1)
                conn, _ = srv.accept()
                data = conn.recv(4096)
                received.append(json.loads(data))
                conn.close()
                srv.close()

            t = threading.Thread(target=run_server)
            t.start()
            time.sleep(0.05)

            send_to_socket(sock_path, {"cook": "eng", "summary": "done"})
            t.join(timeout=2)

            assert len(received) == 1
            assert received[0]["cook"] == "eng"

    def test_silent_on_missing_socket(self, tmp_path):
        # Should not raise — fails silently per spec
        send_to_socket(tmp_path / "nonexistent.sock", {"cook": "eng", "summary": "done"})
