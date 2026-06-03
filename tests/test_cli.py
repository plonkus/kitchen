"""Tests for the kitchen CLI."""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from claude_kitchen.cli import resolve_kitchen, resolve_project, cmd_brigade, cmd_hook, cmd_open, cmd_overview, cmd_hire, cmd_close, _sweep_cooks, cmd_sweep
from claude_kitchen.state import write_status
from claude_kitchen.tmux import CK_PREFIX


class TestResolveKitchen:
    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-risotto"])
    def test_explicit_flag(self, mock_ls):
        assert resolve_kitchen(kitchen="risotto") == "risotto"

    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-risotto"])
    def test_from_env(self, mock_ls, monkeypatch):
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        assert resolve_kitchen() == "risotto"

    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-risotto"])
    def test_single_kitchen(self, mock_ls, monkeypatch):
        monkeypatch.delenv("AGENT_SESSION", raising=False)
        assert resolve_kitchen() == "risotto"

    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-a", "ck-b"])
    def test_ambiguous_raises(self, mock_ls, monkeypatch):
        monkeypatch.delenv("AGENT_SESSION", raising=False)
        with pytest.raises(SystemExit):
            resolve_kitchen()

    def test_rejects_reserved_projects_name(self):
        with pytest.raises(SystemExit, match="reserved"):
            resolve_kitchen(kitchen="projects")

    @patch("claude_kitchen.cli.has_session", return_value=True)
    def test_overview_resolves_as_target(self, mock_has):
        # Unlike "projects", "overview" is a real, targetable kitchen — close /
        # brigade / peek must reach it. resolve_kitchen must NOT reject it.
        assert resolve_kitchen(kitchen="overview") == "overview"


class TestResolveProject:
    def test_existing_directory_resolves(self, tmp_path):
        assert resolve_project(str(tmp_path)) == tmp_path.resolve()

    def test_bare_nonexistent_name_fails_clearly(self):
        with pytest.raises(SystemExit, match="does not resolve to a directory"):
            resolve_project("this-name-is-not-a-real-path-xyz")


def _stdin_payload(monkeypatch, **fields):
    """Fabricate a Claude Code Stop hook stdin payload matching the real shape
    captured against v2.1.112: includes last_assistant_message directly."""
    monkeypatch.setattr(
        "sys.stdin",
        MagicMock(read=MagicMock(return_value=json.dumps(fields))),
    )


def _codex_notify_payload(**fields):
    """Fabricate a Codex notify argv payload matching the real shape captured
    against Codex CLI v0.125.0: hyphenated keys, JSON passed as argv[1]."""
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "00000000-0000-0000-0000-000000000000",
        "turn-id": "00000000-0000-0000-0000-000000000000",
        "cwd": "/repo",
        "client": "codex_exec",
        "input-messages": ["<redacted user prompt>"],
        "last-assistant-message": "<redacted assistant response>",
    }
    payload.update(fields)
    return json.dumps(payload)


class TestHook:
    def test_stop_writes_status_and_sends_to_socket(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message="all tests pass",
            session_id="abc123",
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        status_file = tmp_path / "cooks" / "eng.json"
        data = json.loads(status_file.read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "all tests pass"

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == tmp_path / "kitchen.sock"
        assert call_args[0][1]["cook"] == "eng"
        assert call_args[0][1]["summary"] == "all tests pass"

    def test_sous_stop_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        # Assemble the legacy sentinel at runtime — keeps the residue grep
        # in Task 11 Step 1 clean.
        sentinel = f"[{'AWAITING'}: head-chef decision on rollout]"
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message=f"Phase one done.\n\n{sentinel}",
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        # No cook status file written.
        assert not (tmp_path / "cooks").exists() or not any((tmp_path / "cooks").iterdir())
        # No channel push.
        mock_send.assert_not_called()
        # No marker file written under notes/ (covers the old awaiting-sentinel file).
        notes = tmp_path / "notes"
        assert not notes.exists() or not any(notes.iterdir())

    def test_hook_noop_outside_kitchen(self, monkeypatch):
        monkeypatch.delenv("AGENT_NAME", raising=False)
        monkeypatch.delenv("AGENT_SESSION", raising=False)
        monkeypatch.delenv("STATUS_DIR", raising=False)
        # Should return without error
        cmd_hook(argparse.Namespace(command="hook"))

    def test_user_prompt_submit_writes_working_and_does_not_send(self, monkeypatch, tmp_path):
        """UserPromptSubmit is the canonical 'cook started working' trigger
        for Claude cooks. Must write status='working' and must NOT push to
        the channel socket (it's not a completion event)."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["agent"] == "eng"
        mock_send.assert_not_called()

    def test_hook_ignores_unknown_event_types(self, monkeypatch, tmp_path):
        """Events other than UserPromptSubmit / Stop are ignored — no status
        write, no socket push."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        assert not (tmp_path / "cooks" / "eng.json").exists()
        mock_send.assert_not_called()

    def test_hook_silent_on_bad_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value="not json{")))

        # Should not raise
        cmd_hook(argparse.Namespace(command="hook"))

    def test_codex_notify_writes_status_and_sends_to_socket(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "codex-eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = _codex_notify_payload(
            **{"last-assistant-message": "captured codex summary"}
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        status_file = tmp_path / "cooks" / "codex-eng.json"
        data = json.loads(status_file.read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "captured codex summary"
        assert data["session_id"] == "00000000-0000-0000-0000-000000000000"

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == tmp_path / "kitchen.sock"
        assert call_args[0][1]["cook"] == "codex-eng"
        assert call_args[0][1]["summary"] == "captured codex summary"


def _stage_transcript(tmp_path, *, model, usage):
    """Write a transcript JSONL with one assistant line, structurally
    faithful to the shape captured 2026-04-29 against Claude Code v2.1.119
    (transcript at ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl).
    Returns the path to feed into payload['transcript_path']."""
    line = {
        "type": "assistant",
        "uuid": "00000000-0000-0000-0000-000000000001",
        "sessionId": "00000000-0000-0000-0000-000000000000",
        "version": "2.1.119",
        "message": {
            "model": model,
            "id": "msg_redacted",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "<redacted>"}],
            "usage": usage,
        },
    }
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(line) + "\n")
    return str(transcript)


class TestClaudeStopTokenCapture:
    """Stop hook reads usage from the transcript JSONL the payload points
    at and writes `tokens: {input, max}` to the cook's status. Captured
    payload shape (Claude Code v2.1.119, redacted):

        {
          "session_id": "00000000-0000-0000-0000-000000000000",
          "transcript_path": "/tmp/example/<session-uuid>.jsonl",
          "cwd": "/repo",
          "permission_mode": "bypassPermissions",
          "hook_event_name": "Stop",
          "stop_hook_active": false,
          "last_assistant_message": "<redacted>"
        }

    No `usage` field on the payload itself, hence the JSONL read."""

    def test_stop_writes_tokens_from_transcript(self, monkeypatch, tmp_path):
        # Real input-side numbers from the captured transcript
        # (sum = 1 + 652 + 173374 = 174027 input tokens).
        transcript_path = _stage_transcript(tmp_path, model="claude-opus-4-7",
            usage={
                "input_tokens": 1,
                "cache_creation_input_tokens": 652,
                "cache_read_input_tokens": 173374,
                "output_tokens": 378,
                "service_tier": "standard",
            },
        )

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path,
        )

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}
        assert data["session_id"] == "00000000-0000-0000-0000-000000000000"

    def test_stop_writes_null_max_for_unknown_model(self, monkeypatch, tmp_path):
        transcript_path = _stage_transcript(tmp_path, model="claude-future-9000",
            usage={"input_tokens": 100, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 50},
        )
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path)

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["tokens"] == {"input": 100, "max": None}

    def test_stop_with_missing_transcript_does_not_set_tokens(self, monkeypatch, tmp_path):
        """If the transcript file is missing (or transcript_path is empty),
        the Stop write proceeds without tokens — never crashes."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "nope.jsonl"))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert "tokens" not in data
        assert data["status"] == "idle"
        assert data["summary"] == "hi"


def _stage_codex_rollout(home, thread_id, *, info, day=None):
    """Stage a rollout JSONL under <home>/.codex/sessions/YYYY/MM/DD/.
    Mirrors the structurally-faithful captured shape from a real Codex
    CLI rollout (2026-04-29): event_msg lines wrap a `token_count` payload
    whose `info` field carries `total_token_usage` and
    `model_context_window`. Returns the path created."""
    day = day or datetime.now(timezone.utc).date()
    day_dir = home / ".codex" / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    f = day_dir / f"rollout-2026-04-29T09-49-33-{thread_id}.jsonl"
    lines = [
        # Earlier event with info=null (real shape — rate-limit-only events
        # emit token_count with no usage). Must be skipped, not crashed on.
        {"timestamp": "2026-04-29T16:49:39.675Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": None,
                     "rate_limits": {"limit_id": "codex"}}},
        # The line we actually want — info populated.
        {"timestamp": "2026-04-29T16:53:19.732Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": info,
                     "rate_limits": {"limit_id": "codex"}}},
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


class TestCodexTokenCapture:
    """Codex notify branch reads usage from rollout JSONL.

    Captured 2026-04-29 from a real prolite-plan rollout
    (~/.codex/sessions/2026/04/29/rollout-...-019dda25-574e-...jsonl):

        info.last_token_usage = {
          input_tokens: 67490, cached_input_tokens: 65408,
          output_tokens: 122, reasoning_output_tokens: 45,
          total_tokens: 67612,
        }
        info.total_token_usage = {
          input_tokens: 457361, cached_input_tokens: 401664,
          output_tokens: 5508, reasoning_output_tokens: 2356,
          total_tokens: 462869,   # cumulative running total
        }
        info.model_context_window = 258400

    For rotation decisions we need the CURRENT per-turn context size
    (last_token_usage.input_tokens = 67490), not the cumulative running
    total (total_token_usage.input_tokens = 457361 — meaningless against
    the 258k window since cumulative grows past it on long sessions).
    """

    def test_codex_notify_writes_tokens_from_rollout(self, monkeypatch, tmp_path):
        thread_id = "00000000-0000-0000-0000-000000000000"
        _stage_codex_rollout(tmp_path, thread_id, info={
            "total_token_usage": {
                "input_tokens": 457361,
                "cached_input_tokens": 401664,
                "output_tokens": 5508,
                "reasoning_output_tokens": 2356,
                "total_tokens": 462869,
            },
            "last_token_usage": {
                "input_tokens": 67490, "cached_input_tokens": 65408,
                "output_tokens": 122, "reasoning_output_tokens": 45,
                "total_tokens": 67612,
            },
            "model_context_window": 258400,
        })

        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": thread_id,
            "last-assistant-message": "<redacted>",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert data["tokens"] == {"input": 67490, "max": 258400}
        assert data["session_id"] == thread_id
        assert data["status"] == "idle"

    def test_codex_notify_omits_tokens_when_rollout_missing(self, monkeypatch, tmp_path):
        """No rollout file in the 7-day window → tokens key is omitted
        (not present-and-null). Status update still proceeds normally."""
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": "11111111-1111-1111-1111-111111111111",
            "last-assistant-message": "no rollout staged",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert "tokens" not in data
        assert data["status"] == "idle"
        assert data["summary"] == "no rollout staged"

    def test_codex_notify_skips_info_null_lines(self, monkeypatch, tmp_path):
        """Only the rate-limit-only `info: null` line exists → no usable
        token_count, so tokens are omitted."""
        thread_id = "22222222-2222-2222-2222-222222222222"
        # Stage with info=None for both lines.
        _stage_codex_rollout(tmp_path, thread_id, info=None)

        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": thread_id,
            "last-assistant-message": "info-null only",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert "tokens" not in data


class TestChannelCtxAttribute:
    """Both Stop (Claude) and notify (Codex) branches push `ctx` into the
    channel send_to_socket payload when tokens are present, and omit the
    key entirely when tokens are absent."""

    def test_claude_stop_includes_ctx_when_tokens_present(self, monkeypatch, tmp_path):
        # Stage a transcript with real-shape usage so Stop computes tokens.
        transcript_path = _stage_transcript(tmp_path, model="claude-opus-4-7",
            usage={
                "input_tokens": 1, "cache_creation_input_tokens": 652,
                "cache_read_input_tokens": 173374, "output_tokens": 378,
            })
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="ok",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path)

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        push = mock_send.call_args[0][1]
        # 1 + 652 + 173374 = 174027 → floor(174027 * 100 / 1_000_000) = 17.
        assert push["ctx"] == "17% (174k/1000k)"
        assert push["cook"] == "eng"

    def test_claude_stop_omits_ctx_when_transcript_missing_and_no_prior(self, monkeypatch, tmp_path):
        """No prior tokens, transcript can't be read → ctx omitted entirely."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="ok",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "vanished.jsonl"))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        push = mock_send.call_args[0][1]
        assert "ctx" not in push, f"ctx must be absent, not null/empty; got: {push!r}"

    def test_codex_notify_includes_ctx_when_rollout_present(self, monkeypatch, tmp_path):
        thread_id = "00000000-0000-0000-0000-000000000000"
        _stage_codex_rollout(tmp_path, thread_id, info={
            "total_token_usage": {"input_tokens": 457361, "cached_input_tokens": 401664,
                                  "output_tokens": 5508, "reasoning_output_tokens": 2356,
                                  "total_tokens": 462869},
            "last_token_usage":  {"input_tokens": 67490, "cached_input_tokens": 65408,
                                  "output_tokens": 122, "reasoning_output_tokens": 45,
                                  "total_tokens": 67612},
            "model_context_window": 258400,
        })
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{"thread-id": thread_id,
                                           "last-assistant-message": "<redacted>"})

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        push = mock_send.call_args[0][1]
        # 67490 → floor(67490 * 100 / 258400) = 26.
        assert push["ctx"] == "26% (67k/258k)"

    def test_codex_notify_omits_ctx_when_rollout_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": "11111111-1111-1111-1111-111111111111",
            "last-assistant-message": "no rollout",
        })

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        push = mock_send.call_args[0][1]
        assert "ctx" not in push


class TestStatusPreservationAcrossNonStopWriters:
    """Per spec §Status preservation rules, `tokens` and `backend` survive
    all non-completion status transitions."""

    def test_user_prompt_submit_preserves_tokens_and_backend(self, monkeypatch, tmp_path):
        # Seed a Stop-equivalent state with both durable fields.
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
            "summary": "earlier summary",
        })

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_cmd_ticket_preserves_tokens_and_backend(
        self, mock_rk, mock_state, mock_send, tmp_path,
    ):
        """cmd_ticket flips status→working but must not silently clobber
        durable fields. This is the realistic "ticket fired between turns"
        case for a cook with prior usage."""
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
            "summary": "earlier",
        })

        from claude_kitchen.cli import cmd_ticket
        args = MagicMock()
        args.kitchen = "risotto"
        args.cook = "eng"
        args.message = "next task"
        cmd_ticket(args)

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}

    def test_stop_with_failed_transcript_read_preserves_prior_tokens(self, monkeypatch, tmp_path):
        """Realistic 'turn N succeeded, turn N+1's transcript read fails'
        case. Prior `tokens` must survive — that's the whole reason the
        Stop write goes through update_status instead of write_status."""
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
        })

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        # Transcript path points at a file that doesn't exist — helper
        # returns None, Stop branch must not write tokens (preserving prior).
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="next turn",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "vanished.jsonl"))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "next turn"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}, (
            "Prior tokens must survive a transcript-read miss"
        )

    @patch("claude_kitchen.cli.spawn_window", return_value=False)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_cmd_hire_failure_preserves_backend(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        """If spawn_window fails, the booting write's `backend` must
        survive into the failed write. Symmetric with the other
        non-completion preservation cases."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        with pytest.raises(SystemExit):
            cmd_hire(args)
        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "failed"
        assert data["backend"] == "codex"


class TestBrigadeAlignedOutput:
    """Per spec §Chunk 4: cmd_brigade output is one row per cook,
    column-aligned `<cook-name>  <status>  <ctx>`. Cook-name padded to
    the longest name in the listing; status padded to a fixed 7-char
    width (longest of working/booting/unknown/failed/idle) so ctx
    aligns vertically. No summary suffix."""

    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-risotto"])
    @patch("claude_kitchen.cli.list_windows",
           return_value=["alpha", "longername", "x", "frsh"])
    @patch("claude_kitchen.cli.read_status")
    def test_aligned_columns_three_ctx_cases(self, mock_status, mock_win, mock_sess, capsys):
        # Cook order matches list_windows; mix of statuses + ctx cases.
        mock_status.side_effect = [
            {"status": "idle",   "tokens": {"input": 174027, "max": 1_000_000},
             "summary": "should not appear"},  # summary must be dropped
            {"status": "working","tokens": {"input": 12345,  "max": None}},  # unknown model
            {"status": "booting"},                                            # no tokens
            {"status": "failed", "tokens": {"input": 0, "max": 200_000}},
        ]
        cmd_brigade(kitchen=None)
        out = capsys.readouterr().out

        # Longest name is 'longername' (10 chars); status pad is 7.
        # Two-space separators per the spec.
        assert "  alpha       idle     17% (174k/1000k)" in out, out
        assert "  longername  working  12k (unknown)" in out, out
        assert "  x           booting  —" in out, out
        assert "  frsh        failed   0% (0k/200k)" in out, out
        # Summary is dropped from default brigade output.
        assert "should not appear" not in out
        assert '-- "' not in out


class TestCmdOpen:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_writes_mcp_config_and_execs(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_spawn, mock_slug, mock_ns, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)

        # .mcp.json written to state dir
        mcp_config = tmp_path / ".mcp.json"
        assert mcp_config.exists()
        config = json.loads(mcp_config.read_text())
        assert "kitchen" in config["mcpServers"]

        # spawn_sous called with the namespaced kitchen name, state dir,
        # prompt, and worktree path (worktree keeps the bare `requested` name)
        mock_spawn.assert_called_once()
        call_args = mock_spawn.call_args
        assert call_args[0][0] == "widget-risotto"
        assert call_args[0][1] == tmp_path
        assert call_args[0][3] == Path("/tmp/risotto")

    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp/myproject"))
    def test_rejects_reserved_overview_name(self, mock_resolve):
        # `kitchen open overview` must be rejected at the cmd_open entry point —
        # before any worktree / state-dir / tmux side effects.
        args = MagicMock()
        args.name = "overview"
        args.project = "/tmp/myproject"
        with pytest.raises(SystemExit, match="reserved name"):
            cmd_open(args)


class TestCmdOverview:
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    def test_creates_global_state_and_execs(
        self, mock_tmux, mock_has, mock_spawn, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_tmux.return_value = MagicMock(returncode=0)

        cmd_overview(MagicMock())

        base = tmp_path / ".claude-kitchen" / "overview"
        assert base.is_dir()
        assert (base / "wiki").is_dir()
        assert (base / "notes").is_dir()

        # kitchen.json schema for overview: source/worktree/sous_session_id null,
        # slug pinned to "overview".
        kj = json.loads((base / "kitchen.json").read_text())
        assert kj == {
            "source": None, "slug": "overview",
            "worktree": None, "sous_session_id": None,
        }

        # MCP channel server bound to the overview kitchen.
        cfg = json.loads((base / ".mcp.json").read_text())
        assert cfg["mcpServers"]["kitchen"]["args"] == ["channel-server", "overview"]

        # spawn_sous invoked in overview mode for the global state dir.
        mock_spawn.assert_called_once()
        cargs, ckwargs = mock_spawn.call_args
        assert cargs[0] == "overview"
        assert cargs[1] == base
        assert ckwargs.get("overview") is True

    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    def test_reopen_preserves_kitchen_json(
        self, mock_tmux, mock_has, mock_spawn, tmp_path, monkeypatch,
    ):
        # A second invocation must not clobber a recorded sous_session_id
        # (the pid guard inside spawn_sous is what stops a real re-open).
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_tmux.return_value = MagicMock(returncode=0)
        base = tmp_path / ".claude-kitchen" / "overview"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps(
            {"source": None, "slug": "overview", "worktree": None,
             "sous_session_id": "abc-123"}
        ) + "\n")

        cmd_overview(MagicMock())

        kj = json.loads((base / "kitchen.json").read_text())
        assert kj["sous_session_id"] == "abc-123"


class TestCloseOverview:
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="overview")
    def test_close_overview_null_source_no_crash(
        self, mock_resolve, mock_state, mock_tmux, tmp_path,
    ):
        # Overview's kitchen.json has source=null/worktree=null. cmd_close must
        # not crash on Path(None) and must perform standard teardown.
        base = tmp_path / "overview"
        (base / "cooks").mkdir(parents=True)
        (base / "notes").mkdir()
        mock_state.return_value = base
        (base / "kitchen.json").write_text(json.dumps(
            {"source": None, "slug": "overview", "worktree": None,
             "sous_session_id": None}
        ) + "\n")
        (base / ".mcp.json").write_text("{}")
        (base / "kitchen.sock").write_text("")
        (base / "sous.pid").write_text("123")
        (base / "cooks" / "ghost.json").write_text("{}")

        args = MagicMock()
        args.kitchen = "overview"
        args.keep_worktree = False
        args.force = False

        cmd_close(args)  # must not raise

        # Standard teardown: state files and dirs removed.
        for leftover in ("kitchen.json", ".mcp.json", "kitchen.sock", "sous.pid"):
            assert not (base / leftover).exists(), f"{leftover} should be removed"
        assert not (base / "cooks").exists()
        assert not (base / "notes").exists()
        mock_tmux.assert_called_once()  # kill-session


class TestSweepCooks:
    def _populate(self, base, names):
        cooks = base / "cooks"
        cooks.mkdir(parents=True)
        for n in names:
            (cooks / f"{n}.json").write_text("{}")
        return cooks

    @patch("claude_kitchen.cli.list_windows")
    def test_deletes_orphans_keeps_live(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta", "gamma"])
        mock_lw.return_value = ["alpha"]
        _sweep_cooks(tmp_path, "ck-x")
        survivors = sorted(f.stem for f in cooks.glob("*.json"))
        assert survivors == ["alpha"]

    @patch("claude_kitchen.cli.list_windows")
    def test_noop_when_all_live(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta"])
        mock_lw.return_value = ["alpha", "beta", "unrelated"]
        _sweep_cooks(tmp_path, "ck-x")
        assert sorted(f.stem for f in cooks.glob("*.json")) == ["alpha", "beta"]

    @patch("claude_kitchen.cli.list_windows")
    def test_deletes_all_when_no_live_windows(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta"])
        mock_lw.return_value = []
        _sweep_cooks(tmp_path, "ck-x")
        assert list(cooks.glob("*.json")) == []

    @patch("claude_kitchen.cli.list_windows")
    def test_noop_when_cooks_dir_missing(self, mock_lw, tmp_path):
        mock_lw.return_value = ["alpha"]
        _sweep_cooks(tmp_path, "ck-x")  # must not raise


class TestCmdSweep:
    @patch("claude_kitchen.cli.list_windows", return_value=["alpha"])
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_deletes_ghost_and_prints_summary(
        self, mock_resolve, mock_state, mock_lw, tmp_path, capsys,
    ):
        mock_state.return_value = tmp_path
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "alpha.json").write_text("{}")
        (cooks / "ghost.json").write_text("{}")

        args = MagicMock()
        args.kitchen = None
        cmd_sweep(args)

        assert not (cooks / "ghost.json").exists()
        assert (cooks / "alpha.json").exists()
        out = capsys.readouterr().out
        assert "Swept 1 stale cook(s): ghost" in out


class TestCmdOpenNoOriginRepo:
    def test_succeeds_and_writes_fallback_slug(self, tmp_path, monkeypatch):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        args = MagicMock()
        args.name = None
        args.project = str(repo)
        args.worktree_path = None
        args.resume = False

        state = tmp_path / "state"
        (state / "cooks").mkdir(parents=True)
        (state / "cooks" / "ghost.json").write_text("{}")

        with patch("claude_kitchen.cli.resolve_project", return_value=repo), \
             patch("claude_kitchen.cli.state_dir", return_value=state), \
             patch("claude_kitchen.cli.spawn_sous"), \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.list_windows", return_value=[]), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)):
            cmd_open(args)

        kj = json.loads((state / "kitchen.json").read_text())
        assert kj["slug"], "slug should be derived via full-path fallback, not empty"
        assert "-" in kj["slug"] and kj["slug"].endswith("myrepo")
        assert not (state / "cooks" / "ghost.json").exists(), "stale cook file should be swept"


class TestCmdOpenFailures:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_exits_when_sous_chef_md_missing(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_slug, mock_ns, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        with patch("claude_kitchen.cli._PKG_DIR", tmp_path):
            with pytest.raises(SystemExit, match="sous-chef.md not found"):
                cmd_open(args)


class TestCmdOpenWikiAndNotes:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    def test_creates_wiki_and_notes(
        self, mock_slug, mock_resolve, mock_wt, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path / ".claude-kitchen" / "widget-risotto"
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)

        # Wiki keys off the project slug; notes off the (namespaced) kitchen name.
        wiki = tmp_path / ".claude-kitchen" / "projects" / "widget" / "wiki"
        notes = tmp_path / ".claude-kitchen" / "widget-risotto" / "notes"
        assert (wiki / "mistakes.md").exists()
        assert (wiki / "preferences.md").exists()
        assert (notes / "handoff.md").exists()
        assert (notes / "log.md").exists()

        kj = json.loads((mock_state.return_value / "kitchen.json").read_text())
        assert kj["slug"] == "widget"

    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    def test_resume_reads_stored_slug(
        self, mock_slug, mock_resolve, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / ".claude-kitchen" / "widget-risotto"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget",
        }) + "\n")
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        cmd_open(args)
        assert (tmp_path / ".claude-kitchen" / "projects" / "widget" / "wiki" / "mistakes.md").exists()
        # The resuming branch rewrites kitchen.json with the re-derived slug.
        # Verify the rewrite actually happened and preserved the source field.
        kj = json.loads((base / "kitchen.json").read_text())
        assert kj == {"source": "/tmp/myproject", "slug": "widget"}

    @patch("claude_kitchen.cli.namespaced", return_value="renamed-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="renamed")
    def test_drift_fails_loudly(
        self, mock_slug, mock_resolve, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / ".claude-kitchen" / "renamed-risotto"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget",
        }) + "\n")
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False

        with pytest.raises(SystemExit, match=r"Run `kitchen close renamed-risotto` and reopen\."):
            cmd_open(args)


class TestCmdOpenSoftCutover:
    """A pre-namespacing bare-name kitchen owned by this project is
    re-attached (with a deprecation suggestion) instead of forked."""

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.list_windows", return_value=[])
    @patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0))
    @patch("claude_kitchen.cli.project_slug", return_value="proj")
    @patch("claude_kitchen.cli.resolve_project")
    def test_attaches_legacy_bare_kitchen(
        self, mock_resolve, mock_slug, mock_tmux, mock_lw, mock_spawn, mock_ns,
        tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        proj = tmp_path / "proj"
        proj.mkdir()
        mock_resolve.return_value = proj

        ns_base = tmp_path / ".claude-kitchen" / "proj-foo"      # namespaced: absent
        bare_base = tmp_path / ".claude-kitchen" / "foo"          # legacy: present
        bare_base.mkdir(parents=True)
        (bare_base / "kitchen.json").write_text(json.dumps({
            "source": str(proj), "slug": "old-long-slug",
        }) + "\n")

        def state_dir_for(name):
            return {"proj-foo": ns_base, "foo": bare_base}[name]

        # Only the legacy bare session is live; the namespaced one is not.
        def has_session_for(session):
            return session == "ck-foo"

        args = MagicMock()
        args.name = "foo"
        args.project = str(proj)
        args.worktree_path = None
        args.resume = False

        with patch("claude_kitchen.cli.state_dir", side_effect=state_dir_for), \
             patch("claude_kitchen.cli.has_session", side_effect=has_session_for):
            cmd_open(args)

        out = capsys.readouterr().out
        assert "predates namespacing" in out
        assert "proj-foo" in out
        # Attached to the legacy bare kitchen, not the namespaced fork.
        assert mock_spawn.call_args[0][0] == "foo"
        # No namespaced kitchen forked alongside it.
        assert not (ns_base / "kitchen.json").exists()
        # Legacy slug refreshed to the new short form (drift guard bypassed).
        kj = json.loads((bare_base / "kitchen.json").read_text())
        assert kj["slug"] == "proj"


class TestResolveKitchenProbe:
    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_probes_namespaced_when_bare_absent(self, mock_cwd, mock_ns):
        # bare ck-foo missing, namespaced ck-proj-foo present → resolve to it.
        def has_session_for(session):
            return session == "ck-proj-foo"
        with patch("claude_kitchen.cli.has_session", side_effect=has_session_for):
            assert resolve_kitchen("foo") == "proj-foo"

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_prefers_live_bare_session(self, mock_cwd, mock_ns):
        # A legacy bare session that's live keeps being targeted directly.
        with patch("claude_kitchen.cli.has_session", return_value=True):
            assert resolve_kitchen("foo") == "foo"

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_returns_bare_when_neither_exists(self, mock_cwd, mock_ns):
        with patch("claude_kitchen.cli.has_session", return_value=False):
            assert resolve_kitchen("foo") == "foo"

    @patch("claude_kitchen.cli._cwd_project", return_value=None)
    def test_no_probe_outside_project_root(self, mock_cwd):
        with patch("claude_kitchen.cli.has_session", return_value=False):
            assert resolve_kitchen("foo") == "foo"


class TestCmdHireFailures:
    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=False)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_exits_on_hire_timeout(self, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "claude"
        args.project = None
        args.role = None
        args.effort = None
        with patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp")):
            with pytest.raises(SystemExit, match="didn't show prompt"):
                cmd_hire(args)
        status = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert status["status"] == "failed"


class TestCmdHireRole:
    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_unknown_role_fails_with_valid_list(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "claude"
        args.project = None
        args.role = "ghost"
        args.effort = None
        with pytest.raises(SystemExit, match="Unknown role.*ghost"):
            cmd_hire(args)

    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_role_resolves_and_passes_path_to_spawn(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "rev1"
        args.backend = "claude"
        args.project = None
        args.role = "reviewer"
        args.effort = None
        cmd_hire(args)
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["role_path"] is not None
        assert kwargs["role_path"].name == "reviewer.md"
        assert kwargs["role_path"].exists(), "Resolved role path should exist (Task 11 created it)"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_codex_role_delivered_via_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "rev-codex"
        args.backend = "codex"
        args.project = None
        args.role = "reviewer"
        args.effort = None
        cmd_hire(args)
        # Codex doesn't get a --append-system-prompt-file flag
        assert mock_spawn.call_args.kwargs["role_path"] is None
        # Role content delivered via send_keys after wait_for_prompt
        mock_send.assert_called_once()
        args_call = mock_send.call_args.args
        assert args_call[0] == "ck-risotto"
        assert args_call[1] == "rev-codex"
        # role content + ack footer both arrive in one send
        assert "reviewer" in args_call[2].lower()
        assert "You review code, specs, and plans" in args_call[2]
        assert "Ready, chef." in args_call[2]

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_codex_default_role_sent_via_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "cook1"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        cmd_hire(args)
        mock_send.assert_called_once()
        assert "_default — generic cook" in mock_send.call_args.args[2]

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_claude_does_not_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        # Claude gets role via --append-system-prompt-file; no send_keys needed.
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng1"
        args.backend = "claude"
        args.project = None
        args.role = "eng"
        args.effort = None
        cmd_hire(args)
        mock_send.assert_not_called()


class TestCmdClose:
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_removes_cooks_dir(self, mock_rk, mock_state, mock_tmux, tmp_path):
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "eng.json").write_text("{}")
        args = MagicMock()
        args.kitchen = "risotto"
        cmd_close(args)
        assert not cooks.exists()


class TestCmdCloseWipesNotesNotWiki:
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_close_removes_notes_dir(self, mock_rk, mock_state, mock_tmux, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        base = tmp_path / ".claude-kitchen" / "risotto"
        base.mkdir(parents=True)
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        notes = base / "notes"
        notes.mkdir()
        (notes / "log.md").write_text("stuff")
        wiki = tmp_path / ".claude-kitchen" / "projects" / "acme-widget" / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "mistakes.md").write_text("table")

        args = MagicMock()
        args.kitchen = "risotto"
        args.force = False
        cmd_close(args)

        assert not notes.exists(), "notes/ should be wiped"
        assert (wiki / "mistakes.md").exists(), "project wiki must persist"


class TestCmdSetupExit:
    @patch("claude_kitchen.cli.subprocess.run")
    def test_exits_non_zero_when_claude_missing(self, mock_run, monkeypatch, tmp_path):
        mock_run.return_value = MagicMock(returncode=127, stdout="", stderr="not found")
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            '{"hooks":{"Stop":[{"hooks":[{"command":"kitchen hook"}]}]}}'
        )
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit) as exc:
            cmd_setup(MagicMock())
        assert exc.value.code == 1


class TestCmdSetupStatusline:
    """Three-way advisory: no statusLine, different statusLine (embed advice),
    kitchen segment present (green). None block setup."""

    def _base_settings(self):
        return {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "kitchen hook"}]}],
            }
        }

    def _prep_home(self, tmp_path, settings_obj):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        if settings_obj is not None:
            (claude_dir / "settings.json").write_text(json.dumps(settings_obj))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            '[features]\ncodex_hooks = true\nnotify = ["kitchen", "hook-codex"]\n'
        )
        sp = tmp_path / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers"
        sp.mkdir(parents=True)

    @patch("claude_kitchen.cli.subprocess.run")
    def test_missing_settings_file_advisory(self, mock_run, monkeypatch, tmp_path, capsys):
        """No settings.json → not-configured branch (both options offered)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit):
            cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "Statusline not configured" in out
        # Option 1 (minimal, segment-direct) + Option 2 (richer packaged).
        assert "kitchen statusline-segment" in out
        assert "statusline-command.sh" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_key_missing_advisory_only(self, mock_run, monkeypatch, tmp_path, capsys):
        """Valid settings, no statusLine key → not-configured, but setup still
        exits zero (advisory only)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._prep_home(tmp_path, self._base_settings())

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "Statusline not configured" in out
        assert "🍳 All set" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_points_to_missing_file(self, mock_run, monkeypatch, tmp_path, capsys):
        """Broken path in statusLine.command → treated as not-configured."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = self._base_settings()
        settings["statusLine"] = {
            "type": "command",
            "command": str(tmp_path / "does-not-exist.sh"),
        }
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "Statusline not configured" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_different_not_kitchen(self, mock_run, monkeypatch, tmp_path, capsys):
        """User has their own statusline script, no kitchen segment → embed advice."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        script = tmp_path / "mystatusline.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": str(script)}
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "no kitchen segment detected" in out
        assert "$(kitchen statusline-segment)" in out
        assert "🍳 All set" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_green_when_script_embeds_segment(self, mock_run, monkeypatch, tmp_path, capsys):
        """Script whose text references `kitchen statusline-segment` → green."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        script = tmp_path / ".claude" / "statusline-command.sh"
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": f"bash {script}"}
        self._prep_home(tmp_path, settings)
        script.write_text("#!/bin/sh\n$(kitchen statusline-segment)\n")
        script.chmod(0o755)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "kitchen segment active" in out
        assert "not configured" not in out
        assert "no kitchen segment" not in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_green_when_command_is_segment_directly(self, mock_run, monkeypatch, tmp_path, capsys):
        """Minimal setup: `command: "kitchen statusline-segment"` → green."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": "kitchen statusline-segment"}
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "kitchen segment active" in out

    def test_packaged_script_exists_and_is_executable(self):
        """Guard against the .sh file not shipping with the package."""
        from claude_kitchen.cli import _PKG_DIR
        script = _PKG_DIR / "statusline-command.sh"
        assert script.exists(), f"Packaged statusline missing at {script}"
        assert script.stat().st_mode & 0o111, "Packaged statusline should be executable"

    def test_packaged_script_delegates_to_segment_cli(self):
        """The packaged script must reference `kitchen statusline-segment` —
        otherwise installing it wouldn't satisfy the green branch of the check."""
        from claude_kitchen.cli import _PKG_DIR
        body = (_PKG_DIR / "statusline-command.sh").read_text()
        assert "kitchen statusline-segment" in body


class TestCmdStatuslineSegment:
    """`kitchen statusline-segment` soft-resolves the current kitchen, prints
    one line, and is silent when outside any kitchen."""

    def _populate_cooks(self, base, statuses):
        cooks = base / "cooks"
        cooks.mkdir(parents=True)
        for i, s in enumerate(statuses):
            (cooks / f"cook{i}.json").write_text(json.dumps({"status": s}))

    @patch("claude_kitchen.cli.list_sessions", return_value=[])
    def test_no_kitchen_prints_nothing(self, mock_ls, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_SESSION", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""

    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-a", "ck-b"])
    def test_ambiguous_without_agent_session_is_silent(self, mock_ls, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_SESSION", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""

    @patch("claude_kitchen.cli.state_dir")
    def test_with_agent_session_prints_attach_and_counts(
        self, mock_state, monkeypatch, tmp_path, capsys,
    ):
        monkeypatch.setenv("AGENT_SESSION", "ck-risotto")
        mock_state.return_value = tmp_path
        self._populate_cooks(tmp_path, ["working", "booting", "idle", "failed"])

        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux attach -t ck-risotto ]  [ 2/4 agents active ]"

    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-solo"])
    @patch("claude_kitchen.cli.state_dir")
    def test_single_session_without_agent_session_omits_attach_hint(
        self, mock_state, mock_ls, monkeypatch, tmp_path, capsys,
    ):
        """Called from outside sous (no AGENT_SESSION) but only one kitchen
        is running → segment still renders, but without the attach hint
        since the caller isn't in sous context."""
        monkeypatch.delenv("AGENT_SESSION", raising=False)
        mock_state.return_value = tmp_path
        self._populate_cooks(tmp_path, ["working"])

        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ 1/1 agents active ]"

    @patch("claude_kitchen.cli.state_dir")
    def test_no_cooks_dir_reports_zero_over_zero(
        self, mock_state, monkeypatch, tmp_path, capsys,
    ):
        monkeypatch.setenv("AGENT_SESSION", "ck-empty")
        mock_state.return_value = tmp_path
        # No cooks dir exists.
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux attach -t ck-empty ]  [ 0/0 agents active ]"

    @patch("claude_kitchen.cli.state_dir")
    def test_malformed_cook_json_counts_as_inactive(
        self, mock_state, monkeypatch, tmp_path, capsys,
    ):
        monkeypatch.setenv("AGENT_SESSION", "ck-r")
        mock_state.return_value = tmp_path
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "ok.json").write_text(json.dumps({"status": "working"}))
        (cooks / "broken.json").write_text("not json{")

        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux attach -t ck-r ]  [ 1/2 agents active ]"


class TestCmdRoles:
    def test_lists_roles_with_descriptions(self, capsys):
        from claude_kitchen.cli import cmd_roles
        cmd_roles(MagicMock())
        out = capsys.readouterr().out
        for r in ("_default", "eng", "reviewer", "qa"):
            assert r in out
        assert "implementer" in out  # from eng.md header
        assert "reviewer" in out


class TestBrigadeOutput:
    @patch("claude_kitchen.cli.list_sessions", return_value=["ck-risotto"])
    @patch("claude_kitchen.cli.list_windows", return_value=["cook1", "cook2"])
    @patch("claude_kitchen.cli.read_status")
    def test_brigade_shows_sous_and_cooks(self, mock_status, mock_win, mock_sess, capsys):
        mock_status.side_effect = [
            {"status": "working"},
            {"status": "idle", "summary": "tests pass"},  # summary must be dropped
        ]
        cmd_brigade(kitchen=None)
        out = capsys.readouterr().out
        assert "risotto" in out
        assert "cook1" in out
        assert "cook2" in out
        # Summary suffix removed (Chunk 4).
        assert "tests pass" not in out
        assert '-- "' not in out

