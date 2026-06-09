# Testing

Most of claude-kitchen is covered by `uv run pytest`. A few flows involve real
Claude/Codex processes, tmux, and the dashboard web server, so they're verified
by the manual smokes below.

## Overview v2 dashboard smoke

1. `kitchen open <some-test-kitchen>` — observe `ck-overview` auto-starts
   (check `tmux ls`, expect a `ck-overview` session with `server` + `loop`
   windows). The `loop` window runs the Python daemon (`kitchen overview-loop`),
   not a resident Claude — `tmux capture-pane -t ck-overview:loop -p` should
   show its stderr log, no Claude prompt.
2. Browser: open `http://127.0.0.1:5757/`. Expect the variant-A page (warm
   paper, status-grouped spine: Waiting on you → Working → Booting → Idle, plus
   the dormant drawer toggle) populated from actual kitchen state.
3. Idle tick costs nothing: with no kitchen activity, watch one loop interval
   pass (default 5 min, tunable via `KITCHEN_OVERVIEW_LOOP_MIN`) and confirm no
   `claude -p` process appears (`pgrep -f "claude -p"`). `kitchen
   overview-changes` printing nothing == the gate is empty.
4. Type something in the test kitchen's sous (UserPromptSubmit fires, bumping
   `sous.json`). Wait up to one loop interval, then expect:
   - a structured `~/.claude-kitchen/<name>/synopsis.json` written by a fresh
     one-shot `claude -p` (Opus): envelope (`generated_at` / `based_on_mtime` /
     `kitchen`) + the four judgment fields (`line` / `block` / `actions` /
     `urgency`);
   - the browser receives a WS `loop_tick` event and auto-refetches `/state`;
   - the kitchen's row updates live — `line` for a working/idle kitchen, or the
     `block` headline + ≤3 action steps under "Waiting on you" when blocked.
5. Grouping: a kitchen whose synopsis has `block != null` sits in "Waiting on
   you" regardless of age (never in the dormant drawer); flipping its sous to
   `working` (a new turn starting) moves it to Working on the next re-render.
6. Statusline: confirm the test kitchen's sous pane shows
   `📊 http://127.0.0.1:5757` in its statusline.
7. `kitchen close overview` — verify port 5757 frees and `ck-overview` tears
   down. `synopsis.json` files persist (they're cached state).

If a loop tick fails to fire, inspect the `loop` window's output
(`tmux attach -t ck-overview`, then select the `loop` window) — the Python loop
logs per-kitchen skips and errors to stderr there.
