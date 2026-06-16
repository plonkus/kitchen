# Testing

Unit tests:

```
uv run pytest -q
```

## Manual smoke: `kitchen open --sub-sous` (parent ↔ child both ways)

`--sub-sous` launches a child kitchen whose sous runs **inside the child's own
tmux session** (window `sous`, the `_placeholder` window removed) instead of
replacing the caller's terminal. A parent sous spins one up from a Bash tool
call; the two talk both ways:

- **down** (parent → child): `kitchen ticket sous --kitchen <child> '<msg>'`
  reaches the child sous window (reuses `resolve_kitchen` + `send_keys`; no new
  command).
- **up** (child → parent): the child sous's Stop hook pushes a `<channel>`
  notification to the **parent** kitchen's socket. The wiring is the
  `PARENT_STATUS_DIR` env var the parent injects into the child sous; the
  `cmd_hook` sous carveout forwards the child's Stop there (guarded so a sous
  never pushes to its own socket). `STATUS_DIR` stays the child's own base, so
  the child's own cooks + resume-session capture are unaffected.

### Smoke procedure (use a THROWAWAY parent socket — never the live sous)

To avoid spamming a real channel, point the child at a scratch parent socket
backed by a tiny listener, not a live kitchen's `kitchen.sock`.

1. **Scratch project** — a git repo with one commit:
   ```
   rm -rf /tmp/subsous-smoke && mkdir /tmp/subsous-smoke && cd /tmp/subsous-smoke
   git init -q && git commit -q --allow-empty -m init
   ```

2. **Throwaway parent listener** — bind a unix socket and log what arrives:
   ```
   PARENT=/tmp/subsous-parent && rm -rf "$PARENT" && mkdir -p "$PARENT"
   python3 - "$PARENT/kitchen.sock" "$PARENT/recv.log" <<'PY' &
   import socket, os, sys
   sock, log = sys.argv[1], sys.argv[2]
   try: os.unlink(sock)
   except FileNotFoundError: pass
   s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(sock); s.listen(8)
   with open(log, "a") as f:
       f.write("LISTENER UP\n"); f.flush()
       while True:
           c, _ = s.accept(); data = b""
           while (ch := c.recv(4096)): data += ch
           c.close(); f.write("RECV " + data.decode("utf8","replace").strip() + "\n"); f.flush()
   PY
   ```

3. **Open the child with the sous inside its own session** — `STATUS_DIR`
   points at the throwaway parent base so the child reports UP there:
   ```
   STATUS_DIR="$PARENT" kitchen open --sub-sous
   ```
   Expect: prints `Kitchen "<name>" is open.` + `tmux attach -t ck-<name>`
   (the attach session name the head chef can observe). Blocks until the child
   sous reaches its prompt, then returns.

4. **Verify the session shape** — `sous` window present, `_placeholder` gone:
   ```
   tmux list-windows -t ck-<name> -F '#{window_name}'   # → sous
   ```

5. **Down**: ticket the child sous (it lives in the child session, reached
   cross-kitchen by `--kitchen`):
   ```
   kitchen ticket sous --kitchen <name> 'Liveness check from the head chef. No cooks, no tools — reply with exactly the word PONG, then stop.'
   kitchen peek sous --kitchen <name>     # see the message land + the reply
   ```

6. **Up**: when the child sous finishes that turn it Stops; its hook pushes to
   the parent socket. Confirm:
   ```
   cat "$PARENT/recv.log"   # → RECV {"cook": "<name>", "summary": "...PONG...", ...}
   ```
   `cook` is the child kitchen's name; `summary` is the child sous's last
   message.

7. **Cleanup** — close the child kitchen and stop the listener:
   ```
   kitchen close <name> --force
   kill %1                       # the listener
   rm -rf /tmp/subsous-smoke /tmp/subsous-parent
   ```

A passing smoke shows: the attach session name printed (3), `sous` window with
no `_placeholder` (4), the ticket landing in the child sous pane (5), and a
`RECV` line on the parent socket carrying the child's reply (6).
