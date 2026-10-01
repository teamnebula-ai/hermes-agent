---
type: integration
title: Messaging gateway and platform adapters
description: gateway/run.py, per-platform adapters, the two message guards, and the slash-command registry fan-out.
tags: [integrations, gateway, messaging, telegram, slack, discord]
timestamp: 2026-08-29
---

# Messaging gateway and platform adapters

Not to be confused with `tui_gateway/` (the JSON-RPC backend for the TUI/desktop —
see [architecture/cli-tui-gateway.md](../architecture/cli-tui-gateway.md)). This page
covers `gateway/`, which lets `AIAgent` run behind Telegram, Discord, Slack,
WhatsApp, Home Assistant, Signal, Matrix, Mattermost, email, SMS, DingTalk, WeCom,
WeiXin, Feishu, QQBot, BlueBubbles, Yuanbao, generic webhooks, and a generic
`api_server` adapter (`gateway/platforms/`, 23 files at the gateway root).

## Entry point

`gateway/run.py` (`GatewayRunner`, ~20k LOC) provides `start_gateway()` and is
runnable as `python -m gateway.run` or `python cli.py --gateway`. Its very first
import (before `asyncio`, `json`, etc.) is a defensive one:

```python
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass
```

`hermes_bootstrap` sets up UTF-8 stdio on Windows; the try/except guards a partial
`hermes update` state where a git reset landed new code but `uv pip install -e .`
hadn't finished registering the module yet — POSIX is unaffected either way.

`gateway/session.py` (~1.4k LOC) handles per-conversation session state alongside
the platform-agnostic runner.

## Two message guards — a common bug source

Per `AGENTS.md`, when an agent is actively running, an incoming message passes
through **two sequential guards**, and any new control command that must reach the
runner while the agent is blocked (e.g. an approval prompt) has to bypass **both**,
dispatched inline rather than through `_process_message_background()` (which races
session lifecycle):

1. **Base adapter** (`gateway/platforms/base.py`) — queues messages into
   `_pending_messages` whenever `session_key in self._active_sessions`.
2. **Gateway runner** (`gateway/run.py`) — intercepts `/stop`, `/new`, `/queue`,
   `/status`, `/approve`, `/deny` before they reach `running_agent.interrupt()`.

## Command registry fan-out

All slash commands are `CommandDef` objects in one `COMMAND_REGISTRY` list
(`hermes_cli/commands.py`). Every consumer derives from it automatically — this is
why adding an alias to an existing command requires touching only the `aliases`
tuple:

- **CLI** — `HermesCLI.process_command()` resolves aliases via `resolve_command()`.
- **Gateway** — `GATEWAY_KNOWN_COMMANDS` frozenset gates hook emission;
  `resolve_command()` handles dispatch.
- **Gateway help** — `gateway_help_lines()` generates `/help` output.
- **Telegram** — `telegram_bot_commands()` generates the BotCommand menu.
- **Slack** — `slack_subcommand_map()` generates `/hermes` subcommand routing.
- **Autocomplete** — the flat `COMMANDS` dict feeds `SlashCommandCompleter`.
- **CLI help** — `COMMANDS_BY_CATEGORY` feeds `show_help()`.

`CommandDef` fields: `name` (no leading slash), `description`, `category`
(`Session`/`Configuration`/`Tools & Skills`/`Info`/`Exit`), `aliases`, `args_hint`,
`cli_only`, `gateway_only`, and `gateway_config_gate` — a config dotpath (e.g.
`"display.tool_progress_command"`) that, when set on an otherwise `cli_only`
command, makes it available in the gateway whenever that config value is truthy.
`GATEWAY_KNOWN_COMMANDS` always includes config-gated commands so dispatch works
even when help/menus hide them.

## Adding a new command (4 steps, from `AGENTS.md`)

1. `CommandDef` entry in `COMMAND_REGISTRY` (`hermes_cli/commands.py`).
2. Handler branch in `HermesCLI.process_command()` (`cli.py`).
3. If gateway-available, a matching branch in `gateway/run.py`.
4. For persistent settings, `save_config_value()` in `cli.py`.

## Adding a new platform

Two paths, documented in `gateway/platforms/ADDING_A_PLATFORM.md`:

- **Plugin path (recommended)** — a directory under `~/.hermes/plugins/` (or
  `plugins/platforms/` for bundled ones) with `plugin.yaml` + `adapter.py`. The
  adapter subclasses `BasePlatformAdapter` and registers via
  `ctx.register_platform()` in `register(ctx)` — **zero core changes**. The plugin
  system handles adapter creation, config parsing, user authorization, cron
  delivery, `send_message` routing, system-prompt hints, status display, and setup.
  Optional hooks cover edge cases: `env_enablement_fn` (seed `PlatformConfig` from
  env vars before construction), `apply_yaml_config_fn` (translate the platform's
  own `config.yaml` keys), `cron_deliver_env_var` (wire `deliver=<name>` cron jobs
  without touching `cron/scheduler.py`'s hardcoded sets), `standalone_sender_fn`
  (out-of-process delivery for cron jobs running outside the gateway process).
- **Core path** — for platforms that ship in-tree, following the pattern of the
  existing adapters under `gateway/platforms/`.

## Profile safety for platform adapters

Any adapter that connects with a unique credential (bot token, API key) must call
`acquire_scoped_lock()` from `gateway.status` in `connect()`/`start()` and
`release_scoped_lock()` in `disconnect()`/`stop()`, so two profiles can't collide on
the same credential — see `gateway/platforms/telegram.py` for the canonical
pattern, and [data-models/config-state-profiles.md](../data-models/config-state-profiles.md)
for what profiles are.

## Related

- [architecture/cli-tui-gateway.md](../architecture/cli-tui-gateway.md) — the
  interactive surfaces this gateway parallels (and must not be confused with).
- [integrations/plugins-and-providers.md](plugins-and-providers.md) — the general
  plugin system `register_platform()` belongs to.
- [workflows/automation-cron-kanban.md](../workflows/automation-cron-kanban.md) —
  `deliver=<name>` cron routing into a live platform session.
