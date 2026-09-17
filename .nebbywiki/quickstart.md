---
type: overview
title: Hermes Agent — Quickstart
description: Entry point to the Hermes Agent codebase wiki — what it is, how the pieces fit, and where to go next.
tags: [overview, entrypoint]
timestamp: 2026-08-29
---

# Hermes Agent

Hermes Agent (`hermes-agent`, PyPI/repo name) is Nous Research's AI agent framework —
described in `pyproject.toml` as "the self-improving AI agent — creates skills from
experience, improves them during use, and runs anywhere." It is one Python core
(`run_agent.py`'s `AIAgent`) driven by four different front doors — a classic CLI, an
Ink-based terminal UI, an Electron desktop app, and a multi-platform messaging gateway
(Telegram, Slack, Discord, WhatsApp, and a dozen more) — plus a scheduler (`cron/`) and
a multi-agent work queue (`plugins/kanban/`) that can drive the same core headlessly.

## Why it's built this way

The repo's own dev guide, [`AGENTS.md`](../AGENTS.md), is unusually detailed and is
treated here as a primary source, not just a hint — read it directly for anything
this wiki doesn't cover. Two structural decisions show up everywhere in the code:

- **One core, many surfaces.** `run_agent.py`'s `AIAgent` class and the
  `model_tools.py` → `tools/registry.py` dispatch chain are shared by the CLI, the
  TUI's Python backend, the gateway, `batch_runner.py`, and cron/kanban workers. New
  capability goes into the core once; every surface picks it up.
- **Extend via plugins, not core edits.** Messaging platforms, memory backends,
  model-inference providers, tools, and CLI subcommands all have a plugin registration
  path (`hermes_cli/plugins.py`'s `PluginContext`) so third parties — and most internal
  contributors — never touch `run_agent.py`, `cli.py`, or `gateway/run.py` directly.
  See [integrations/plugins-and-providers.md](integrations/plugins-and-providers.md).

## Map

- [architecture/overview.md](architecture/overview.md) — system diagram: entrypoints,
  the core dependency chain, where state lives.
- [architecture/agent-loop.md](architecture/agent-loop.md) — `AIAgent.run_conversation`,
  tool dispatch, and `delegate_task` subagents.
- [architecture/cli-tui-gateway.md](architecture/cli-tui-gateway.md) — the three
  interactive chat surfaces (classic CLI, Ink TUI, Electron desktop) and how the
  dashboard embeds the TUI instead of re-implementing it.
- [integrations/messaging-platforms.md](integrations/messaging-platforms.md) — the
  gateway's platform adapters (Telegram, Slack, Discord, ...) and the slash-command
  registry that fans out to all of them.
- [integrations/plugins-and-providers.md](integrations/plugins-and-providers.md) — the
  general plugin system, memory-provider and model-provider plugin surfaces, and
  skills vs. optional-skills.
- [data-models/config-state-profiles.md](data-models/config-state-profiles.md) —
  `config.yaml`/`.env`, the three config loaders, `SessionDB` (SQLite+FTS5), and
  profile (`HERMES_HOME`) isolation.
- [workflows/automation-cron-kanban.md](workflows/automation-cron-kanban.md) — the
  cron scheduler and the kanban multi-agent work queue, both of which can drive
  `AIAgent` without a human at the keyboard.
- [operations/testing-and-release.md](operations/testing-and-release.md) — the
  hermetic test wrapper, dependency-pinning policy, and how builds/releases ship
  (PyPI/CalVer, Docker, desktop installers, Homebrew).

## Backlog

Not covered in this pass — worth a follow-up build if these become load-bearing:

- `acp_adapter/` (ACP server for VS Code/Zed/JetBrains) — only surveyed at a glance.
- `tools/environments/` backends (docker, ssh, modal, daytona, singularity,
  managed_modal) — listed but not individually documented.
- `agent/` internals beyond the agent loop and memory (context compression, curator
  backup, credential pooling, LSP support, provider adapters per-vendor) — `agent/`
  has ~88 files; only the pieces load-bearing for the pages above were read.
- `website/` (Docusaurus docs site) — the user-facing docs referenced throughout this
  wiki (e.g. `website/docs/user-guide/features/curator.md`) were not themselves read.
- `web/` and `apps/desktop/` UI internals (React component structure, routing) — the
  desktop app's process model is covered; its component tree is not.
- Skill authoring mechanics beyond the policy summary — see `AGENTS.md`'s "Skills"
  section and `skills/`/`optional-skills/` directly for the full authoring standard.
