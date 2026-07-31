"""Tests for tmux helpers. Uses mocked subprocess calls."""
import subprocess
from unittest.mock import patch, MagicMock
import pytest
from claude_kitchen.tmux import (
    mc, bare, list_kitchens, list_windows,
    has_session, capture_pane, SESSION,
)

CK_PREFIX = "ck-"


def _mock_run(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


class TestNaming:
    def test_mc_adds_prefix(self):
        assert mc("risotto") == "ck-risotto"

    def test_mc_idempotent(self):
        assert mc("ck-risotto") == "ck-risotto"

    def test_bare_strips_prefix(self):
        assert bare("ck-risotto") == "risotto"

    def test_bare_noop_without_prefix(self):
        assert bare("risotto") == "risotto"


def _seed_kitchens(root, *names):
    """Lay out state dirs the way `kitchen open` does: <root>/<name>/kitchen.json."""
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "kitchen.json").write_text("{}\n")


class TestListSessions:
    """No shared server to enumerate any more: candidates come from disk and
    each is probed on its OWN socket."""

    @patch("claude_kitchen.tmux.has_session", return_value=True)
    def test_lists_kitchens_from_disk(self, mock_has, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_kitchens(tmp_path / ".claude-kitchen", "risotto", "bolognese")
        assert list_kitchens() == ["bolognese", "risotto"]

    @patch("claude_kitchen.tmux.has_session", return_value=True)
    def test_ignores_dirs_without_kitchen_json(self, mock_has, tmp_path, monkeypatch):
        # `projects/` (the wiki root) and a closed kitchen (kitchen.json unlinked)
        # both live under the state root but are not kitchens.
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".claude-kitchen"
        _seed_kitchens(root, "risotto")
        (root / "projects" / "widget" / "wiki").mkdir(parents=True)
        (root / "closed").mkdir()
        assert list_kitchens() == ["risotto"]

    @patch("claude_kitchen.tmux.has_session", return_value=False)
    def test_dead_server_is_omitted(self, mock_has, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_kitchens(tmp_path / ".claude-kitchen", "risotto")
        assert list_kitchens() == []

    def test_empty_when_no_state_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert list_kitchens() == []

    @patch("claude_kitchen.tmux.tmux")
    def test_wedged_socket_does_not_block_the_others(self, mock_tmux, tmp_path, monkeypatch):
        """THE regression this design exists for: one kitchen's tmux server
        stops answering. Its probe times out, and every OTHER kitchen still
        lists. Each probe carries the short PROBE_TIMEOUT, so the cost is
        bounded per kitchen instead of hanging the listing."""
        from claude_kitchen.tmux import PROBE_TIMEOUT
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed_kitchens(tmp_path / ".claude-kitchen", "alive", "wedged")

        def probe(*args, kitchen, timeout=None, **kwargs):
            if kitchen == "wedged":
                raise subprocess.TimeoutExpired(cmd="tmux", timeout=timeout)
            return _mock_run(returncode=0)

        mock_tmux.side_effect = probe
        assert list_kitchens() == ["alive"]
        assert all(c.kwargs["timeout"] == PROBE_TIMEOUT
                   for c in mock_tmux.call_args_list)


class TestSocketRouting:
    """Every tmux invocation must carry `-L ck-<kitchen>` — the whole point of
    the design. Asserted on the real argv, one layer below the tmux() wrapper."""

    @patch("claude_kitchen.tmux.subprocess.run")
    def test_tmux_prepends_the_kitchen_socket(self, mock_run):
        from claude_kitchen.tmux import tmux
        mock_run.return_value = _mock_run()
        tmux("has-session", "-t", SESSION, kitchen="ck-risotto")
        assert mock_run.call_args.args[0][:3] == ["tmux", "-L", "ck-risotto"]

    @patch("claude_kitchen.tmux.subprocess.run")
    def test_bare_kitchen_name_resolves_to_the_ck_socket(self, mock_run):
        from claude_kitchen.tmux import tmux
        mock_run.return_value = _mock_run()
        tmux("kill-session", "-t", SESSION, kitchen="risotto")
        assert mock_run.call_args.args[0][:3] == ["tmux", "-L", "ck-risotto"]

    def test_session_is_required(self):
        # Keyword-only and mandatory, so no call can silently land back on the
        # shared default socket.
        from claude_kitchen.tmux import tmux
        with pytest.raises(TypeError):
            tmux("list-sessions")


class TestAttachCmd:
    def test_prints_the_full_socket_qualified_form(self):
        from claude_kitchen.tmux import attach_cmd
        assert attach_cmd("risotto") == "tmux -L ck-risotto attach"

    def test_idempotent_on_an_already_prefixed_name(self):
        from claude_kitchen.tmux import attach_cmd
        assert attach_cmd("ck-risotto") == "tmux -L ck-risotto attach"


class TestListWindows:
    @patch("claude_kitchen.tmux.tmux")
    def test_returns_window_names(self, mock_tmux):
        mock_tmux.return_value = _mock_run("sous\neng\nreviewer\n")
        result = list_windows("ck-risotto")
        assert result == ["sous", "eng", "reviewer"]

    @patch("claude_kitchen.tmux.tmux")
    def test_propagates_tmux_failure(self, mock_tmux):
        # list_windows passes check=True so tmux errors surface, not silently
        # degrade to []. Critical for _sweep_cooks — an empty list means "no
        # live cooks," a tmux failure must not be conflated with that.
        mock_tmux.side_effect = subprocess.CalledProcessError(1, "tmux")
        with pytest.raises(subprocess.CalledProcessError):
            list_windows("ck-missing")


class TestHasSession:
    @patch("claude_kitchen.tmux.tmux")
    def test_true_when_exists(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=0)
        assert has_session("ck-risotto") is True

    @patch("claude_kitchen.tmux.tmux")
    def test_false_when_missing(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=1)
        assert has_session("ck-risotto") is False


class TestCapturePane:
    @patch("claude_kitchen.tmux.tmux")
    def test_captures_output(self, mock_tmux):
        mock_tmux.return_value = _mock_run("hello world\n❯ ")
        result = capture_pane("ck-risotto", "eng")
        assert "hello world" in result

    @patch("claude_kitchen.tmux.tmux")
    def test_returns_none_on_failure(self, mock_tmux):
        mock_tmux.return_value = _mock_run(returncode=1)
        assert capture_pane("ck-risotto", "eng") is None


class TestWaitForPrompt:
    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_confirms_dev_channels_dialog_for_claude(self, mock_cap, mock_tmux, mock_sleep):
        """A claude sous launched with --dangerously-load-development-channels
        shows a confirmation dialog before its welcome banner; wait_for_prompt
        must auto-confirm it (bare Enter on the pre-selected option 1)."""
        from claude_kitchen.tmux import wait_for_prompt
        mock_cap.side_effect = [
            "WARNING: Loading development channels\n  ❯ 1. I am using this for local development",
            "Claude Code v2.1.120",
        ]
        assert wait_for_prompt("ck-x", "sous", "claude", timeout=5) is True
        sent = [c.args for c in mock_tmux.call_args_list]
        assert ("send-keys", "-t", "kitchen:sous", "Enter") in sent

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_no_dialog_no_keystrokes_for_claude(self, mock_cap, mock_tmux, mock_sleep):
        """Welcome banner already up → no dialog → no keystrokes sent."""
        from claude_kitchen.tmux import wait_for_prompt
        mock_cap.side_effect = ["Claude Code v2.1.120"]
        assert wait_for_prompt("ck-x", "sous", "claude", timeout=5) is True
        mock_tmux.assert_not_called()

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_tolerates_transient_tmux_timeout(self, mock_cap, mock_tmux, mock_sleep):
        """A TimeoutExpired from tmux under launch load is transient: the wait
        loop must swallow it and keep polling, not crash the open."""
        from claude_kitchen.tmux import wait_for_prompt
        mock_cap.side_effect = [
            subprocess.TimeoutExpired(cmd="tmux", timeout=15),
            "Claude Code v2.1.120",
        ]
        assert wait_for_prompt("ck-x", "sous", "claude", timeout=5) is True

    @patch("claude_kitchen.tmux.time")
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_gives_up_on_stall_not_full_ceiling(self, mock_cap, mock_tmux, mock_time):
        """An unchanging pane (no marker) is truly stuck → give up after
        stall_timeout, WITHOUT waiting the (much larger) hard ceiling."""
        from claude_kitchen.tmux import wait_for_prompt
        clock = [0]
        def now():
            clock[0] += 10
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.return_value = "booting… (no banner, frozen)"
        # Hard ceiling 100000 so the STALL (not the ceiling) is what returns.
        assert wait_for_prompt("ck-x", "sous", "claude",
                               timeout=100000, stall_timeout=45) is False
        assert clock[0] < 1000, "should give up at the stall, not crawl to the ceiling"

    @patch("claude_kitchen.tmux.time")
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_slow_but_progressing_boot_reaches_prompt(self, mock_cap, mock_tmux, mock_time):
        """A pane that keeps CHANGING is making progress (slow boot under load):
        the wait must NOT give up before the banner finally appears — even across
        many ticks that a flat short cap would have killed."""
        from claude_kitchen.tmux import wait_for_prompt
        clock = [0]
        def now():
            clock[0] += 10  # 10s per call → tens of seconds pass between frames
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        # Distinct frames (progress) for a long time, then the welcome marker.
        mock_cap.side_effect = [f"boot frame {i}" for i in range(8)] + ["Claude Code v2.1.178"]
        assert wait_for_prompt("ck-x", "sous", "claude",
                               timeout=100000, stall_timeout=45) is True


# Real pane captures from the B3 live verification (claude+codex+gemini cooks,
# ck-sbusy). These are the empirical fixtures the busy/idle logic is pinned to.
CLAUDE_IDLE = (
    "  By making information abundant rather than scarce, it reshaped society.\n"
    "✻ Churned for 54s\n"
    "────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────\n"
    "  ██░░░░░░░░░░ 4%  claude-opus-4-8[1m]  main\n"
    "  [ tmux attach -t ck-sbusy ]  [ 0/2 agents active ]\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
# Turn-start spinner (rotating asterisk glyph + gerund + ellipsis).
CLAUDE_BUSY_SPINNER = (
    "  the copying of words but fundamentally restructured knowledge.\n"
    "✻ Churned for 54s\n"
    "❯ Now, without tools, write another 20-paragraph essay.\n"
    "✽ Crafting…\n"
)
CLAUDE_BUSY_SPINNER_SUFFIX = "✶ Fluttering… (1s · ↓ 1 tokens)\n"
# Two frames of a claude STREAMING text: no spinner, footer byte-identical to
# idle — only the growing transcript differs.
CLAUDE_STREAM_1 = (
    "  insulator, it became pliable when heated and hard when cooled.\n"
    "────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────\n"
    "  ██░░░░░░░░░░ 4%  claude-opus-4-8[1m]  main\n"
    "  [ tmux attach -t ck-sbusy ]  [ 1/2 agents active ]\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
CLAUDE_STREAM_2 = CLAUDE_STREAM_1.replace(
    "  insulator, it became pliable when heated and hard when cooled.\n",
    "  insulator, it became pliable when heated and hard when cooled.\n"
    "  The Gutta Percha Company in London became a vital supplier.\n",
)
CODEX_IDLE = (
    "  making radio one of the most adaptable communication technologies.\n"
    "› Explain this codebase\n"
    "  gpt-5.5 high fast · /tmp/proj · Context 98% left\n"
)
CODEX_BUSY = (
    "  making radio one of the most adaptable communication technologies.\n"
    "› Without tools, write a 25-paragraph essay on the history of clocks.\n"
    "• Working (2s • esc to interrupt)\n"
    "› Explain this codebase\n"
    "  gpt-5.5 high fast · /tmp/proj · Context 98% left\n"
)
GEMINI_IDLE = (
    "  It continues to be the ultimate symbol of direction and orientation.\n"
    ">\n"
    "? for shortcuts                                  Gemini 3.5 Flash (Medium)\n"
)
GEMINI_BUSY = (
    "> Without tools, write a 20-paragraph essay on the history of compasses.\n"
    "⣻  Working...\n"
    ">\n"
    "esc to cancel                                    Gemini 3.5 Flash (Medium)\n"
)


def _tail(raw):
    """Mirror _pane_tail's filtering so marker tests can run on raw captures."""
    return [ln for ln in raw.splitlines() if ln.strip()][-40:]


class TestBusyMarkerHit:
    """Pure marker-match logic against the real captured tails."""

    def test_claude_spinner_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CLAUDE_BUSY_SPINNER), "claude") is True

    def test_claude_spinner_with_token_suffix_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CLAUDE_BUSY_SPINNER_SUFFIX), "claude") is True

    def test_claude_idle_completion_line_does_NOT_match(self):
        # "✻ Churned for 54s" has the same glyph as the live spinner but no "…".
        # The ellipsis is the discriminator — this is the brittle bit, pin it.
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(["✻ Churned for 54s"], "claude") is False

    def test_claude_idle_footer_does_NOT_match(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CLAUDE_IDLE), "claude") is False

    def test_codex_working_footer_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CODEX_BUSY), "codex") is True

    def test_codex_idle_does_NOT_match(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(CODEX_IDLE), "codex") is False

    def test_gemini_cancel_footer_matches(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(GEMINI_BUSY), "gemini") is True

    def test_gemini_idle_does_NOT_match(self):
        from claude_kitchen.tmux import _busy_marker_hit
        assert _busy_marker_hit(_tail(GEMINI_IDLE), "gemini") is False

    def test_unknown_backend_raises_valueerror(self):
        from claude_kitchen.tmux import _busy_marker_hit
        with pytest.raises(ValueError, match="unknown backend"):
            _busy_marker_hit(["whatever"], "llama")


class TestPaneBusy:
    """Full pane_busy: marker fast-path, claude change-detection, codex/gemini
    no-fallback, unknown-backend error. capture_pane mocked — no live tmux."""

    @patch("claude_kitchen.tmux.capture_pane")
    def test_codex_busy_marker_is_instant_single_capture(self, mock_cap):
        # Marker hit → True with exactly ONE capture (no settle, no fallback).
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CODEX_BUSY
        assert pane_busy("ck-x", "cx", "codex") is True
        assert mock_cap.call_count == 1

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_codex_idle_no_fallback_single_capture(self, mock_cap, mock_sleep):
        # Persistent-footer backend: marker-miss is authoritatively idle —
        # exactly ONE capture, and NO settle sleep.
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CODEX_IDLE
        assert pane_busy("ck-x", "cx", "codex") is False
        assert mock_cap.call_count == 1
        mock_sleep.assert_not_called()

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_gemini_idle_no_fallback_single_capture(self, mock_cap, mock_sleep):
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = GEMINI_IDLE
        assert pane_busy("ck-x", "gm", "gemini") is False
        assert mock_cap.call_count == 1
        mock_sleep.assert_not_called()

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_claude_idle_stable_pane_is_not_busy(self, mock_cap, mock_sleep):
        # Markerless backend, marker-miss → change-detection. Idle pane is stable
        # across the settle window, so the two captures match → False (no
        # false-positive). Two captures taken.
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CLAUDE_IDLE
        assert pane_busy("ck-x", "cc", "claude") is False
        assert mock_cap.call_count == 2

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.capture_pane")
    def test_claude_streaming_detected_by_change(self, mock_cap, mock_sleep):
        # Markerless backend streaming text: no spinner, footer identical to
        # idle, but the transcript grows between captures → busy.
        from claude_kitchen.tmux import pane_busy
        mock_cap.side_effect = [CLAUDE_STREAM_1, CLAUDE_STREAM_2]
        assert pane_busy("ck-x", "cc", "claude") is True
        assert mock_cap.call_count == 2

    @patch("claude_kitchen.tmux.capture_pane")
    def test_claude_spinner_marker_is_instant(self, mock_cap):
        # Spinner present → True via the marker fast-path, single capture.
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CLAUDE_BUSY_SPINNER
        assert pane_busy("ck-x", "cc", "claude") is True
        assert mock_cap.call_count == 1

    @patch("claude_kitchen.tmux.capture_pane")
    def test_unknown_backend_raises_valueerror(self, mock_cap):
        from claude_kitchen.tmux import pane_busy
        mock_cap.return_value = CLAUDE_IDLE
        with pytest.raises(ValueError, match="unknown backend"):
            pane_busy("ck-x", "cc", "llama")


# ---- B1: verified-submit loop (cursor-column accept predicate) ----
# send_keys: paste (untouched) → settle-poll → Enter-until-the-composer-accepts.
# Acceptance is proven POSITIVELY by the input cursor returning to the empty
# composer column (submit OR queue), NOT by a row string changing — so a repaint
# after a swallowed Enter cannot false-positive into silent ticket loss. These
# drive the real loop with capture_pane (settle) and tmux() (cursor_x + Enter)
# mocked — no live tmux.

_SETTLE_HEAD = "DOTHING"      # ticket text; head == text so it's easy to embed
_EMPTY_COL = 2               # empty-composer cursor column
_PAYLOAD_COL = 37            # cursor at the end of a pasted payload


def _settle_stable():
    """capture_pane side-effect: a baseline with NO head, then identical panes
    that DO contain the head → paste positively observed AND pane stabilizes."""
    calls = {"n": 0}

    def cap(session, window, full=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return "idle transcript\n> "
        return f"transcript line\n> {_SETTLE_HEAD} landed\n"  # head present, stable

    return cap


def _settle_no_signal(session, window, full=False):
    """Settle-poll never sees the head → paste never positively observed."""
    return "idle transcript, head never appears\n> "


_SPINNER_GLYPHS = "✻✽✶✳✢"


def _settle_spinner():
    """capture_pane side-effect modelling a mid-turn claude cook: the payload IS
    in the composer, but the working spinner repaints the pane on EVERY poll —
    rotating glyph, ticking elapsed timer, climbing token count.

    Measured on live cooks (ck-domo-source:eng-x3, ck-watchmepivot:finisher,
    ck-kitchen-claude-kitchen:wedge): the spinner repaints every ~200-333 ms,
    while the settle poll interval is 50 ms + the cost of a tmux call — median
    23-103 ms at rest but spiking past 400 ms on a loaded box. Once the poll
    interval passes ~1/3 of the spinner period, no two consecutive captures
    match and whole-pane stability is unreachable. The composer is quiet
    throughout: it is only the transcript/footer that moves."""
    calls = {"n": 0}

    def cap(session, window, full=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return "idle transcript\n> "
        g = _SPINNER_GLYPHS[calls["n"] % len(_SPINNER_GLYPHS)]
        return (f"{g} Infusing… ({calls['n']}s · ↓ {calls['n']}.1k tokens)\n"
                f"> {_SETTLE_HEAD} landed\n")

    return cap


def _tmux_cursor(empty_col=_EMPTY_COL, settle_col=_PAYLOAD_COL,
                 after_enter=(_EMPTY_COL,)):
    """tmux() side-effect scripted by PHASE, not by raw call count.

    The settle-poll samples the cursor an unbounded number of times (it polls
    until the column holds steady), so a test must not have to know how many
    reads that takes. display-message therefore returns:

      - `empty_col` for the FIRST read — send_keys' pre-paste empty-composer
        sample;
      - `settle_col` for every read before the first Enter — the settle-poll
        and the landed precondition. Pass a sequence to script a cursor that is
        still MOVING (a paste mid-flight); the last entry then repeats.
      - `after_enter[i]` for the single read that follows Enter i+1; the last
        entry repeats, so a steady post-Enter column needs only one value.

    send-keys/load-buffer/paste-buffer → success. Records Enters and the
    load-buffer input."""
    settle = [settle_col] if isinstance(settle_col, int) else list(settle_col)
    after = list(after_enter)
    reads = {"n": 0, "settle": 0}
    state = {"enter": 0, "pasted_text": None}

    def fake(*args, **kwargs):
        cmd = args[0]
        m = MagicMock(returncode=0, stdout="")
        if cmd == "display-message":
            reads["n"] += 1
            if reads["n"] == 1:
                value = empty_col
            elif state["enter"] == 0:
                value = settle[min(reads["settle"], len(settle) - 1)]
                reads["settle"] += 1
            else:
                value = after[min(state["enter"] - 1, len(after) - 1)]
            m.stdout = f"{value}\n"
        elif cmd == "load-buffer":
            state["pasted_text"] = kwargs.get("input")
        elif cmd == "send-keys":
            state["enter"] += 1
        return m

    fake.state = state
    return fake


class TestSendKeysVerifiedSubmit:
    @pytest.mark.parametrize("backend", ["codex", "claude"])
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_offscreen_head_uses_cursor_as_landed_signal(
            self, mock_time, mock_cap, mock_tmux, backend):
        # A long inline multiline paste can push its head above the visible tmux
        # viewport. The pane therefore never exposes either textual landing
        # signal, even though cursor_x proves the payload is in the composer.
        from claude_kitchen.tmux import send_keys
        clock = [0.0]

        def now():
            clock[0] += 0.05
            return clock[0]

        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        head = "OFFSCREEN_HEAD: do thing"
        padding = [f"padding line {i:02d} abcdef" for i in range(1, 40)]
        text = "\n".join([head] + padding)
        visible_tail = "\n".join(padding[-10:])
        captures = {"n": 0}

        def offscreen_pane(session, window, full=False):
            captures["n"] += 1
            return "idle transcript\n> " if captures["n"] == 1 else visible_tail

        assert len(text) < 1000
        assert head[:24] not in visible_tail
        mock_cap.side_effect = offscreen_pane
        # cursor_x: empty (pre-paste) → payload (landing signal) → payload
        # (settled precondition) → empty (accepted after Enter).
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake

        send_keys("ck-x", "cx", text, backend=backend)

        assert fake.state["enter"] == 1
        assert fake.state["pasted_text"] == text

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_first_enter_submits(self, mock_cap, mock_tmux, mock_sleep):
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        # cursor_x: empty (pre-paste) → payload (precondition) → empty (accepted).
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 1

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_swallowed_first_enter_is_retried(self, mock_cap, mock_tmux, mock_sleep):
        # codex paste-commit race: Enter #1 swallowed (cursor still at payload),
        # Enter #2 lands (cursor back to empty). The loop recovers.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor(after_enter=(
            _PAYLOAD_COL,    # after Enter #1: swallowed, still in composer
            _EMPTY_COL,      # after Enter #2: accepted
        ))
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 2

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_repaint_without_acceptance_does_not_false_positive(self, mock_cap, mock_tmux, mock_sleep):
        # CRITICAL 2: the payload is NEVER accepted (cursor never returns to the
        # empty column) but the composer "repaints" — the cursor jitters across
        # columns (wrap/stub-expansion/redraw). The OLD row-string predicate would
        # have declared success (row changed); the cursor-column predicate must
        # NOT — it raises rather than silently losing the ticket.
        from claude_kitchen.tmux import send_keys, _SUBMIT_ATTEMPTS
        mock_cap.side_effect = _settle_stable()
        jitter = [40, 38, 41, 39, 42, 37, 40, 38]  # never _EMPTY_COL
        fake = _tmux_cursor(after_enter=jitter[:_SUBMIT_ATTEMPTS])
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="never left the composer"):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == _SUBMIT_ATTEMPTS

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_queued_while_busy_counts_as_success(self, mock_cap, mock_tmux, mock_sleep):
        # TRAP guard: a follow-up fired mid-turn is QUEUED, not committed to
        # history — but the composer DOES clear (cursor → empty column), so the
        # predicate correctly counts it as accepted. (Identical cursor signature
        # to a normal submit: that is the point — accept == submit OR queue.)
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 1

    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_paste_never_observed_raises_without_any_enter(self, mock_time, mock_cap, mock_tmux):
        # CRITICAL 1 (never signalled): settle deadline with no paste-landed
        # signal → raise, send NO Enter (never submit blind).
        from claude_kitchen.tmux import send_keys
        clock = [0.0]
        def now():
            clock[0] += 0.5
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.side_effect = _settle_no_signal
        # Cursor remains empty throughout the settle window too; a moved codex
        # cursor is now independent positive proof that the paste landed.
        fake = _tmux_cursor(settle_col=_EMPTY_COL)
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="did not settle"):
            send_keys("ck-x", "cx", "a ticket whose head never lands", backend="codex")
        assert fake.state["enter"] == 0

    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_observed_but_composer_still_moving_raises(self, mock_time, mock_cap, mock_tmux):
        # CRITICAL 1: paste IS observed but the COMPOSER never comes to rest —
        # the cursor keeps advancing because Ink is still streaming the payload
        # in. Submitting now would race the remainder, so raise, send NO Enter.
        #
        # "Still landing" is a property of the composer, not of the pane: the
        # settle-poll must watch the cursor column. (Was
        # test_observed_but_never_stable_raises, which asserted a repainting
        # PANE should raise — that pinned the bug rather than the contract, see
        # test_spinner_repaint_does_not_block_settle.)
        from claude_kitchen.tmux import send_keys
        clock = [0.0]
        def now():
            clock[0] += 0.2
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.side_effect = _settle_stable()   # pane quiet; only the cursor moves
        # Longer than the whole poll budget, so the cursor is still advancing
        # when the loop gives up — "never comes to rest", not "rests late".
        fake = _tmux_cursor(settle_col=tuple(range(_PAYLOAD_COL, _PAYLOAD_COL + 300, 3)))
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="did not settle"):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 0

    @pytest.mark.parametrize("backend", ["codex", "claude"])
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_spinner_repaint_does_not_block_settle(self, mock_time, mock_cap, mock_tmux, backend):
        # REGRESSION (the wedge): a cook mid-turn repaints its pane on every
        # single poll while its composer sits perfectly still holding the
        # payload. Whole-pane stability is unreachable there, so the old check
        # raised "did not settle" and ABANDONED the payload in the composer —
        # which then poisoned the next send's empty-column baseline. Settling on
        # the cursor column instead is immune: the spinner never moves it.
        from claude_kitchen.tmux import send_keys
        clock = [0.0]
        def now():
            clock[0] += 0.2
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.side_effect = _settle_spinner()
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend=backend)
        assert fake.state["enter"] == 1

    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_slow_tmux_does_not_starve_the_poll_loop(self, mock_time, mock_cap, mock_tmux):
        # REGRESSION: the loop needs a MINIMUM of four iterations to conclude
        # anything — one to observe the paste, three to watch the cursor hold
        # still. A wall-clock budget does not guarantee them: on a loaded box a
        # single `tmux display-message` has been measured at 2.5s (load 87, 68
        # users, ~92 cook windows), which burns a 2.0s budget before the first
        # poll even completes. The paste is fine and the composer is at rest;
        # the loop just never got to look. Budgeting in POLLS guarantees it does.
        #
        # Each clock tick here stands for one poll costing 2.5s of wall time.
        from claude_kitchen.tmux import send_keys
        clock = [0.0]
        def now():
            clock[0] += 2.5
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="claude")
        assert fake.state["enter"] == 1

    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    @patch("claude_kitchen.tmux.time")
    def test_hung_tmux_hits_the_ceiling_instead_of_hanging(self, mock_time, mock_cap, mock_tmux):
        # The poll budget must not become an unbounded wait: a tmux server that
        # is HUNG (not merely busy) would otherwise hold `kitchen ticket` for
        # _SETTLE_POLLS * the per-call TIMEOUT. The wall-clock ceiling stays as
        # a backstop — it fires only in that pathological case, and the raise
        # reports the short poll count so the two are distinguishable.
        from claude_kitchen.tmux import send_keys, _SETTLE_POLLS
        clock = [0.0]
        def now():
            clock[0] += 60.0  # one poll = a full minute → nothing is coming back
            return clock[0]
        mock_time.time.side_effect = now
        mock_time.sleep.return_value = None
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match=r"did not settle.*polls=\d+/%d" % _SETTLE_POLLS):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="claude")
        assert fake.state["enter"] == 0

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_paste_did_not_land_in_composer_raises(self, mock_cap, mock_tmux, mock_sleep):
        # Precondition: if after a 'settled' paste the cursor is STILL at the
        # empty column, the payload never reached the composer — raise rather than
        # let the loop's 'back to empty' check fire on a no-op.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor(settle_col=_EMPTY_COL)  # precondition still empty
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="did not land in the composer"):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 0

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_never_re_pastes_only_enters(self, mock_cap, mock_tmux, mock_sleep):
        # IRON RULE: retry is Enter-only — after the initial paste-buffer there is
        # NO further load-buffer/paste-buffer no matter how many Enter retries run.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor(after_enter=(_PAYLOAD_COL, _EMPTY_COL))
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        cmds = [c.args[0] for c in mock_tmux.call_args_list]
        assert cmds.count("paste-buffer") == 1
        assert cmds.count("load-buffer") == 1
        assert fake.state["enter"] == 2  # retried, never re-pasted

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_trailing_newlines_stripped_before_paste(self, mock_cap, mock_tmux, mock_sleep):
        # A trailing newline would add an empty final composer row whose cursor
        # sits back at the empty column, defeating the accept check (and the codex
        # role-ack footer ends in "\n"). The text must be rstrip("\n")ed before
        # the paste; INTERNAL newlines are preserved.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor()
        mock_tmux.side_effect = fake
        send_keys("ck-x", "cx", "DOTHING\nsecond line\n\n", backend="codex")
        assert fake.state["pasted_text"] == "DOTHING\nsecond line"

    # ---- cycle-3: pin the two residual column-predicate edges ----
    # Both proven non-reproducible on real cooks (codex + claude): a NON-EMPTY
    # payload always parks the cursor at column >= empty_col + 1 (content floor,
    # verified across both 80-col wrap boundaries and short multi-line tails),
    # so the cursor never collides with empty_col while text is present. These
    # two tests pin the boundary so it stays explicit.

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_text_remaining_is_not_false_accepted(self, mock_cap, mock_tmux, mock_sleep):
        # EDGE 2 (residual false-positive / silent-loss class): a swallowed Enter
        # leaves the payload in the composer. "Text remains" means the cursor
        # stays at a CONTENT column — even the closest content can get to empty,
        # the floor empty_col+1 (a 1-char / wrapped tail) — never AT empty_col.
        # So the predicate must NOT accept: it keeps Entering and ultimately
        # raises, never declaring a still-unsent ticket delivered.
        from claude_kitchen.tmux import send_keys, _SUBMIT_ATTEMPTS
        floor = _EMPTY_COL + 1  # the lowest column real content ever occupies
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor(settle_col=floor, after_enter=(floor,))
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="never left the composer"):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == _SUBMIT_ATTEMPTS

    @patch("claude_kitchen.tmux.time.sleep", return_value=None)
    @patch("claude_kitchen.tmux.tmux")
    @patch("claude_kitchen.tmux.capture_pane")
    def test_landing_at_empty_col_fails_loud(self, mock_cap, mock_tmux, mock_sleep):
        # EDGE 1 (landing false-negative): IF a real payload ever landed reading
        # cursor_x == empty_col (a hypothetical exact wrap-to-empty-col that the
        # floor invariant says doesn't happen), the landed precondition raises
        # "did not land" — fail LOUD, send NO Enter. The sous sees the error and
        # retries; the ticket is never blindly submitted or silently lost.
        from claude_kitchen.tmux import send_keys
        mock_cap.side_effect = _settle_stable()
        fake = _tmux_cursor(settle_col=_EMPTY_COL)  # post-settle cursor still at empty col
        mock_tmux.side_effect = fake
        with pytest.raises(RuntimeError, match="did not land in the composer"):
            send_keys("ck-x", "cx", _SETTLE_HEAD, backend="codex")
        assert fake.state["enter"] == 0
