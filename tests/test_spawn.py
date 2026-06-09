"""Tests for spawn logic."""
import os
import shlex
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from claude_kitchen.spawn import build_shell_cmd, spawn_sous, spawn_overview_loop


def _codex_argv_from_shell_cmd(cmd: str) -> list[str]:
    """Reproduce what codex sees as argv after bash -lc parsing the shell
    command produced by build_shell_cmd. The outer string is
    `bash -lc '<inner>'`; bash -lc would parse <inner> through shell rules.
    shlex.split with posix=True matches that parsing for our quoting shapes."""
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-lc"], f"unexpected outer shape: {outer[:2]}"
    inner_tokens = shlex.split(outer[2])
    # The inner is `export ...; exec codex <args...>`. Find `exec codex` and
    # return what follows as codex's argv (program + args).
    i = inner_tokens.index("exec")
    return inner_tokens[i + 1:]


class TestBuildShellCmd:
    def test_claude_cook(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "claude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "AGENT_NAME=" in cmd
        assert "eng" in cmd

    def test_codex_cook(self):
        cmd = build_shell_cmd(
            backend="codex", name="reviewer", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "codex" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_codex_cook_has_notify_override(self):
        """Codex cook argv must contain `-c notify=["kitchen","hook-codex"]`
        as two adjacent tokens, surviving the bash -lc shell-quoting layer.
        Bypasses any global notify wrapper (SkyComputerUseClient et al.)."""
        cmd = build_shell_cmd(
            backend="codex", name="rev", session="ck-r",
            status_dir="/tmp/state",
        )
        argv = _codex_argv_from_shell_cmd(cmd)
        assert argv[0] == "codex"
        # Find -c notify=... in argv (there may be other -c flags too).
        notify_seen = False
        for i, tok in enumerate(argv):
            if tok == "-c" and i + 1 < len(argv) and argv[i + 1].startswith("notify="):
                assert argv[i + 1] == 'notify=["kitchen","hook-codex"]', (
                    f"notify override value malformed: {argv[i+1]!r}"
                )
                notify_seen = True
                break
        assert notify_seen, f"-c notify=... not found in codex argv: {argv}"

    def test_claude_cook_has_no_notify_override(self):
        """Claude has no equivalent notify mechanism; the override is codex-only."""
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-r",
            status_dir="/tmp/state",
        )
        assert "notify=" not in cmd, "notify override leaked to claude cook"

    def test_codex_notify_override_coexists_with_effort(self):
        """Effort flag and notify override must both make it through, in
        the right argv shape (each behind its own -c)."""
        cmd = build_shell_cmd(
            backend="codex", name="rev", session="ck-r",
            status_dir="/tmp/state", effort="high",
        )
        argv = _codex_argv_from_shell_cmd(cmd)
        # Two -c flags expected, in order: model_reasoning_effort, notify
        c_pairs = [
            (argv[i], argv[i + 1])
            for i in range(len(argv) - 1)
            if argv[i] == "-c"
        ]
        keys = [pair[1].split("=", 1)[0] for pair in c_pairs]
        assert "model_reasoning_effort" in keys
        assert "notify" in keys

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            build_shell_cmd(
                backend="gpt", name="eng", session="ck-risotto",
                status_dir="/tmp/state",
            )

    def test_propagates_kitchen_prefixed_env(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_NOTES", "/tmp/n")
        monkeypatch.setenv("KITCHEN_WIKI", "/tmp/w")
        monkeypatch.setenv("FOO", "bar")
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "KITCHEN_NOTES=/tmp/n" in cmd
        assert "KITCHEN_WIKI=/tmp/w" in cmd
        assert "FOO=bar" not in cmd

    def test_no_kitchen_env_omits_vars(self, monkeypatch):
        for k in list(os.environ):
            if k.startswith("KITCHEN_"):
                monkeypatch.delenv(k)
        cmd = build_shell_cmd(
            backend="codex", name="eng", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "KITCHEN_" not in cmd


class TestRoleInjection:
    def test_claude_role_passes_file_path(self, tmp_path):
        role_file = tmp_path / "roles" / "eng.md"
        role_file.parent.mkdir()
        role_file.write_text("# eng — implementer\nbody with 'quotes' & specials\nmultiline")
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-r",
            status_dir="/tmp/state", role_path=role_file,
        )
        assert "--append-system-prompt-file" in cmd
        assert str(role_file) in cmd
        # Contents must NOT be inlined — that would re-introduce the quoting risk
        assert "implementer" not in cmd
        assert "multiline" not in cmd

    def test_claude_no_role_omits_flag(self):
        cmd = build_shell_cmd(
            backend="claude", name="cook1", session="ck-r",
            status_dir="/tmp/state",
        )
        assert "--append-system-prompt-file" not in cmd


class TestSpawnSous:
    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_chdir_to_project(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", project=Path("/tmp/myproject"), slug="gh-x-y")
        mock_chdir.assert_called_once_with(Path("/tmp/myproject"))

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_no_chdir_when_no_project(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", slug="gh-x-y")
        mock_chdir.assert_not_called()

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_exports_dashboard_url(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        # Every sous gets KITCHEN_DASHBOARD_URL so its statusline can surface the
        # dashboard; the port follows KITCHEN_DASHBOARD_PORT.
        monkeypatch.setenv("KITCHEN_DASHBOARD_PORT", "6060")
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        monkeypatch.delenv("KITCHEN_DASHBOARD_URL", raising=False)
        spawn_sous("risotto", tmp_path, "prompt", slug="gh-x-y")
        assert os.environ["KITCHEN_DASHBOARD_URL"] == "http://127.0.0.1:6060"
        monkeypatch.setenv("KITCHEN_DASHBOARD_URL", os.environ["KITCHEN_DASHBOARD_URL"])

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_remote_control_enabled_with_kitchen_prefix(
        self, mock_exec, mock_chdir, tmp_path, monkeypatch,
    ):
        """Sous launches with --remote-control + prefix=<kitchen>. The pair
        must appear as adjacent argv tokens — claude parses the flag value
        positionally."""
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt", slug="gh-x-y")
        argv = mock_exec.call_args.args[1]
        assert "--remote-control" in argv
        i = argv.index("--remote-control-session-name-prefix")
        assert argv[i + 1] == "risotto"

    def test_cook_argv_has_no_remote_control(self):
        """Cooks must NOT get --remote-control; the flag is sous-only."""
        for backend in ("claude", "codex"):
            cmd = build_shell_cmd(
                backend=backend, name="eng", session="ck-r",
                status_dir="/tmp/state",
            )
            assert "--remote-control" not in cmd, f"{backend} cook leaked RC flag"

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_exports_wiki_and_notes_env(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("KITCHEN_WIKI", raising=False)
        monkeypatch.delenv("KITCHEN_NOTES", raising=False)
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        state = tmp_path / ".claude-kitchen" / "risotto"
        state.mkdir(parents=True)
        spawn_sous(
            "risotto", state,
            "prompt", project=Path("/tmp/p"), slug="gh-acme-widget",
        )
        import os
        assert os.environ["KITCHEN_WIKI"] == str(
            tmp_path / ".claude-kitchen" / "projects" / "gh-acme-widget" / "wiki"
        )
        assert os.environ["KITCHEN_NOTES"] == str(
            tmp_path / ".claude-kitchen" / "risotto" / "notes"
        )
        # Register post-spawn values with monkeypatch so its teardown restores them
        monkeypatch.setenv("KITCHEN_WIKI", os.environ["KITCHEN_WIKI"])
        monkeypatch.setenv("KITCHEN_NOTES", os.environ["KITCHEN_NOTES"])


class TestSpawnOverviewLoop:
    @patch("claude_kitchen.spawn.tmux")
    def test_launches_server_and_loop_windows_with_env(self, mock_tmux, tmp_path):
        # The detached ck-overview session has two windows: the FastAPI server
        # and the Python summarizer loop (NO resident Claude). The `loop` window
        # execs `kitchen overview-loop` directly. Assert the exact tmux
        # invocations + command/env shape.
        spawn_overview_loop(tmp_path, port="5757")

        calls = mock_tmux.call_args_list
        assert len(calls) == 2

        # window 1: the FastAPI server, as a fresh DETACHED session
        server = calls[0].args
        assert server[0] == "new-session" and "-d" in server
        assert server[server.index("-s") + 1] == "ck-overview"
        assert server[server.index("-n") + 1] == "server"
        assert server[server.index("-c") + 1] == str(tmp_path)
        server_cmd = server[-1]
        assert "kitchen dashboard-server" in server_cmd

        # window 2: the Python loop, named `loop`, added to the same session
        loop = calls[1].args
        assert loop[0] == "new-window"
        assert loop[loop.index("-t") + 1] == "ck-overview"
        assert loop[loop.index("-n") + 1] == "loop"
        assert loop[loop.index("-c") + 1] == str(tmp_path)
        loop_cmd = loop[-1]
        assert "exec kitchen overview-loop" in loop_cmd
        assert "claude" not in loop_cmd          # no resident Claude

        # both windows export the dashboard env; AGENT_NAME is gone (no sous)
        for cmd in (server_cmd, loop_cmd):
            assert "AGENT_NAME=" not in cmd
            assert "AGENT_SESSION=ck-overview" in cmd
            assert f"STATUS_DIR={tmp_path}" in cmd
            assert "KITCHEN_DASHBOARD_URL=http://127.0.0.1:5757" in cmd
            assert "KITCHEN_DASHBOARD_PORT=5757" in cmd
            assert f"KITCHEN_NOTES={tmp_path / 'notes'}" in cmd
            assert f"KITCHEN_WIKI={tmp_path / 'wiki'}" in cmd

    @patch("claude_kitchen.spawn.tmux")
    def test_port_flows_into_url_and_env(self, mock_tmux, tmp_path):
        spawn_overview_loop(tmp_path, port="6001")
        loop_cmd = mock_tmux.call_args_list[1].args[-1]
        assert "KITCHEN_DASHBOARD_URL=http://127.0.0.1:6001" in loop_cmd
        assert "KITCHEN_DASHBOARD_PORT=6001" in loop_cmd
