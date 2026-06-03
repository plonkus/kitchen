# Testing

Most of claude-kitchen is covered by `uv run pytest`. A few flows involve real
Claude/Codex processes and tmux, so they're verified by the manual smokes below.

## Overview mode

The overview kitchen (`kitchen overview`) is a read-only meta-sous that watches
every open kitchen on the machine. Smoke it like this:

1. **Open it.** From any cwd:

   ```
   kitchen overview
   ```

   It boots at `~/.claude-kitchen/overview/`, starts its own channel server,
   and the sous ingests current state (`kitchen overview-snapshot` + recent
   transcript slices). Its first reply digests the live kitchens by name and
   ends with a `KITCHEN STATUS` footer. Dormant kitchens (idle > 24h) collapse
   into a single `… and N dormant kitchens (idle > 24h)` line.

2. **Forward a Stop.** With overview open and at least one other kitchen
   running, get that kitchen's sous to respond to you. Within a few seconds the
   overview conversation shows a channel notification:

   ```
   ← kitchen: <name>  stop → <the sous's last message>
   ─── KITCHEN STATUS ──────────
   …
   ```

   `cook` is the source kitchen name; the body is the event line plus the fresh
   deterministic footer. Typing in that other kitchen instead forwards a
   `prompt → …` line and flips its footer row to `working`.

3. **`status` shortcut.** Type `status` (or `?`) to overview during a quiet
   moment — it replies with the fresh footer only, no prose.

4. **Self-loop safety.** Reply to a message inside the overview sous itself
   (firing its own Stop). It must NOT post a `← kitchen: overview` notification
   back into its own conversation, and must NOT loop — it returns to idle.

5. **Close it.** 

   ```
   kitchen close overview
   ```

   Tears down the session, socket, and state files. With overview closed, other
   kitchens' Stop/UserPromptSubmit events silently no-op the forward branch —
   their souses are unaffected.
