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

If `$AGENT_SESSION` is set you are already the sous of a running kitchen — do not run `kitchen open`. If it is not set, a human opens the kitchen.
