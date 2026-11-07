# Testing

Unit tests:

```
uv run pytest -q
```

## Sous ↔ sous tickets

A ticket goes through the target's `kitchen.sock`, so it reaches a sous running in
a terminal, with no tmux window to paste into. Captured with two sous opened in
panes of a private tmux server, with a throwaway `HOME` and `TMUX_TMPDIR`.

The receiver takes the ticket and answers it (`bravo`'s pane):

```
← kitchen: What is 17 + 25? Answer in one line.
⏺ Bash(kitchen ticket sous --kitchen alpha "17 + 25 = 42")
  ⎿  Ticket fired to the sous of alpha. Heard!
```

Check the sender's pane too: the round trip is proven only when the reply lands
there, and the exchange ends without a further ticket (`alpha`'s pane):

```
← kitchen: 17 + 25 = 42
⏺ Bravo's sous answered: 17 + 25 = 42. That was a reply to the ticket I sent, so
  I sent nothing back.
```

A ticket that lands mid-turn shows at once and is handled when the running tool
call returns; it does not interrupt the tool. A target with no listener exits 1
and writes no status.
