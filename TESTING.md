# Testing

Most of claude-kitchen is covered by `uv run pytest`. A few flows involve real
Claude/Codex processes, tmux, and the dashboard web server, so they're verified
by the manual smokes below.

## Overview v2 dashboard smoke

1. `kitchen open <some-test-kitchen>` — observe `ck-overview` auto-starts
   (check `tmux ls`, expect a `ck-overview` session with `server` + `loop`
   windows).
2. Browser: open `http://127.0.0.1:5757/`. Expect a status grid populated from
   actual kitchen state. The footer shows a "live" indicator (WS connected).
3. Type something in the test kitchen's sous (UserPromptSubmit fires). Wait up
   to 5 min for the loop tick, then expect:
   - `synopsis.md` written for that kitchen (`~/.claude-kitchen/<name>/synopsis.md`);
   - the browser receives a WS `loop_tick` event and auto-refetches `/state`;
   - the dashboard updates the synopsis text live.
4. Statusline: confirm the test kitchen's sous pane shows
   `📊 http://127.0.0.1:5757` in its statusline.
5. `kitchen close overview` — verify port 5757 frees and `ck-overview` tears
   down. Synopsis files persist (they're cached state).

If a loop tick fails to fire, inspect the `loop` window's output
(`tmux attach -t ck-overview`, then select the `loop` window) — the Python loop
logs per-kitchen skips and errors to stderr there.
