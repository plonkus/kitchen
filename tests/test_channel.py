"""Tests for the kitchen channel MCP server."""
import asyncio
import json
import socket as sock_mod
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_kitchen.channel import _claim_socket, handle_connection, send_to_socket


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


class TestClaimSocket:
    """Test the connect-probe guard that refuses to stomp a live owner."""

    def test_no_existing_socket_is_noop(self, tmp_path):
        # Nothing there → just proceed to bind (no exit, nothing created).
        sock_path = Path("/tmp") / "ck-claim-absent.sock"
        sock_path.unlink(missing_ok=True)
        _claim_socket(sock_path)  # returns cleanly
        assert not sock_path.exists()

    def test_live_owner_refused_no_unlink(self):
        # A live listener owns the socket → stand down (exit 0), socket
        # untouched (same inode/owner), and it still answers afterward.
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            sock_path = Path(td) / "k.sock"
            srv = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.listen(5)
            try:
                before = sock_path.stat().st_ino

                with pytest.raises(SystemExit) as exc:
                    _claim_socket(sock_path)
                assert exc.value.code == 0

                # Socket file unchanged (not unlinked/rebound): same inode.
                assert sock_path.exists()
                assert sock_path.stat().st_ino == before

                # Routing round-trip: the ORIGINAL owner still accepts. Drain
                # the probe's now-closed leftover connection if it lingers.
                client = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
                client.connect(str(sock_path))
                client.sendall(b"ping\n")
                srv.settimeout(2)
                got = None
                while got is None:
                    conn, _ = srv.accept()
                    data = conn.recv(16)
                    conn.close()
                    if data == b"ping\n":
                        got = data
                assert got == b"ping\n"
                client.close()
            finally:
                srv.close()

    def test_stale_socket_unlinked_and_recovered(self):
        # A dead/stale socket file (bound then closed, no listener) → connect
        # gives ECONNREFUSED → unlink and let the caller bind. No regression
        # to cold start.
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            sock_path = Path(td) / "k.sock"
            dead = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            dead.bind(str(sock_path))
            dead.close()  # leaves the file on disk, nothing listening
            assert sock_path.exists()

            _claim_socket(sock_path)  # returns cleanly, no exit
            assert not sock_path.exists(), "stale socket must be unlinked"

    def test_other_oserror_fails_loud_no_unlink(self, tmp_path, capsys):
        # An unexpected OSError on probe (e.g. EACCES / ENOTSOCK) must NOT
        # unlink (could destroy something we don't understand) and must fail
        # loud with a non-zero exit.
        sock_path = tmp_path / "mystery.sock"
        sock_path.write_text("not a socket")  # exists, but not ours to delete

        class FakeSock:
            def connect(self, _):
                raise PermissionError("EACCES")

            def close(self):
                pass

        with patch("claude_kitchen.channel.sock_mod.socket", return_value=FakeSock()):
            with pytest.raises(SystemExit) as exc:
                _claim_socket(sock_path)
        assert exc.value.code == 1
        assert sock_path.exists(), "must NOT unlink on unexpected OSError"
        err = capsys.readouterr().err
        assert "refusing to bind" in err
