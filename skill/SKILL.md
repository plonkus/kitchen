---
name: claude-kitchen
description: Use when setting up, managing, or orchestrating multi-agent workflows with the kitchen CLI. Also use when asked to hire cooks, check brigade status, or send tickets.
---

# claude-kitchen

Multi-agent orchestration over tmux: a sous chef coordinates cook agents, one per tmux window.

```bash
kitchen hire <name>           # spawn a cook
kitchen ticket <cook> "..."   # send it work
```

Hiring carries no task — the ticket is always a separate command.

Run `kitchen --help` for the current flags and `kitchen roles` for the available cook roles, and prefer those over anything remembered. This file lists no flags on purpose: that list is what drifts.

`$AGENT_KITCHEN` is the only signal that you are inside a kitchen — cooks are given no kitchen MCP tools, so reasoning from their absence gets you a confident wrong answer. Set, and `$AGENT_NAME` is anything but `sous`: you are a cook in a running kitchen — do the work in your ticket and report back, and do not hire, clock out or open. Set, and `$AGENT_NAME` is `sous`: the kitchen is already open, so don't reopen it. Unset: a human opens the kitchen. (`$AGENT_SESSION` is the pre-#17 spelling and is no longer exported; keying off it never fires.)
