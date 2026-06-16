# Testing

Unit tests:

```
uv run pytest -q
```

## `kitchen open --sub-sous` — parent ↔ child, both ways

`--sub-sous` launches a child kitchen whose sous runs **inside the child's own
tmux session** (window `sous`, the `_placeholder` window removed) instead of
replacing the caller's terminal. A parent sous spins one up from a Bash tool
call; the two talk both ways:

- **down** (parent → child): `kitchen ticket sous --kitchen <child> '<msg>'`
  reaches the child sous window (reuses `resolve_kitchen` + `send_keys`).
- **up** (child → parent): the child sous's Stop hook pushes a `<channel>`
  notification to the **parent** kitchen's socket, via the `PARENT_STATUS_DIR`
  env var the parent injects (the `cmd_hook` sous carveout, guarded so a sous
  never pushes to its own socket). `STATUS_DIR` stays the child's own base.

Fresh opens only (rejects `--resume` / an existing kitchen/session). On a
genuine launch failure the half-created kitchen is torn down (session +
worktree + branch + state) — it never leaves a sous-less "open" kitchen.

### Smoke procedure (THROWAWAY parent socket — never the live sous)

Point the child at a scratch parent socket backed by a tiny listener so a real
channel is never spammed:

```
# scratch project + throwaway parent listener
rm -rf /tmp/subsous-smoke && mkdir /tmp/subsous-smoke && cd /tmp/subsous-smoke
git init -q && git commit -q --allow-empty -m init
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

# open the child with its sous inside its own session; report UP to $PARENT
STATUS_DIR="$PARENT" kitchen open --sub-sous

# down: ticket the child sous cross-kitchen, then watch the reply
kitchen ticket sous --kitchen <name> 'Liveness check from the head chef. No cooks, no tools — reply with exactly the word PONG, then stop.'
kitchen peek sous --kitchen <name>

# up: the child's Stop pushes to the parent socket
cat "$PARENT/recv.log"

# cleanup
kitchen close <name> --force; kill %1; rm -rf /tmp/subsous-smoke /tmp/subsous-parent
```

### Captured run (real output)

`kitchen open --sub-sous` (attach session name printed; reaches prompt):

```
Kitchen "private-tmp-subsous-smoke-subsous-smoke" is open. Sous chef on the line.
   tmux attach -t ck-private-tmp-subsous-smoke-subsous-smoke
```

Window shape — `sous` present, `_placeholder` gone
(`tmux list-windows -t ck-private-tmp-subsous-smoke-subsous-smoke`):

```
2:sous
```

**Down** — `kitchen ticket sous --kitchen <name> '…PONG…'` lands in the child
sous pane and it replies (`kitchen peek sous --kitchen <name>`):

```
❯ Liveness check from the head chef — do NOT hire cooks, do NOT run tools or bash. Just reply with exactly the single word PONG, then stop.
⏺ PONG
✻ Crunched for 2s
```

**Up** — the child sous's Stop pushed to the throwaway parent socket
(`cat $PARENT/recv.log`):

```
RECV {"cook": "private-tmp-subsous-smoke-subsous-smoke", "summary": "PONG", "ts": "2026-06-16T00:38:48Z"}
```

`cook` is the child kitchen's name; `summary` is the child sous's last message.

## Fail-clean & concurrency (hardening)

Two `kitchen open --sub-sous` at once used to crash both at the hard 5s tmux
timeout, leaving a half-open kitchen. Now: the per-call tmux timeout is 15s,
`wait_for_prompt` swallows a transient `TimeoutExpired` and retries, and any
genuine failure tears the half-created kitchen down.

`wait_for_prompt` is also **progress-based**: under heavy machine load the whole
child-sous boot (window → dev-channels confirm dialog → bare-Enter → render the
banner) can far exceed any flat cap — diagnosed live on a load-50 box where a
healthy sous simply booted slowly (the dialog IS dismissed by the Enter; it's
not auth/crash). So it now waits as long as the pane keeps changing (progress)
and gives up only after `stall_timeout` of no change (truly stuck) or a generous
hard ceiling, instead of a fixed 60s.

Two `--sub-sous` guards protect the abort path: `--sub-sous` refuses when a
worktree/branch named `<name>` already exists (so `_abort_sub_sous` can never
force-remove a worktree/branch it didn't create), and a `list-panes` timeout
after a successful `new-window` no longer tears down a live sous (the pid write
is best-effort). Verified:

**Genuine-failure teardown** — drive `cmd_open --sub-sous` with
`wait_for_prompt` forced to `False` so a real session + worktree + branch +
state are created, then must all be removed:

```
SYSTEMEXIT: --sub-sous: the child sous never reached its prompt; cleaned up kitchen "private-tmp-subsous2-failinj".
✓ no session    ✓ no worktree dir    ✓ no branch    ✓ no state dir    ✓ no stray process
```

**Two sequential opens** — each fully succeeds (`sous` window, `sous.pid`
alive):

```
Kitchen "private-tmp-subsous2-seqa" is open. Sous chef on the line.
Kitchen "private-tmp-subsous2-seqb" is open. Sous chef on the line.
  seqa: windows=sous  sous.pid ALIVE
  seqb: windows=sous  sous.pid ALIVE
```

**Two near-simultaneous opens** (separate repos, shared tmux server — the crash
scenario) — both fully succeed, neither crashes or leaves junk:

```
Kitchen "private-tmp-subsous2-subsous2" is open. Sous chef on the line.   EXIT=0
Kitchen "private-tmp-subsous3-subsous3" is open. Sous chef on the line.   EXIT=0
  both: windows=sous  sous.pid ALIVE
```
