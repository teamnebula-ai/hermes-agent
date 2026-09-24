#!/usr/bin/env python3
"""Last-resort alert for when a health check could not do its job.

Wired as ``OnFailure=`` on every health-check unit, so it fires whenever a check
exits non-zero: DISARMED, "alerted but nothing was delivered", a crash, or a
timeout. Those are the cases where the watchdog knows something is wrong and
cannot tell anyone through its normal channels.

Until 2026-08-17 this did not exist. The check's own docstring claimed exit 1 was
"paired with OnFailure= in the systemd unit" and no unit had ever carried the
directive, so a failed delivery reached stderr, marked the unit failed, and
stopped there. You found out by running `systemctl --user status`, which nobody
runs while everything looks fine.

WHY TELEGRAM. It has to be a transport that cannot fail for the same reason the
primary did. A second email address would not qualify: both would authenticate
with TMN_COMPOSIO_API_KEY, so one rotation takes out both. The Hermes Telegram
bot token is a different secret from a different vendor, it already lives on both
boxes, and it fails visibly -- if that token dies, the bot stops answering and
you notice within minutes.

NOTHING HERE IMPORTS THE CHECK. A last-resort notifier that depends on the
component which just failed is not a last resort. Config parsing, env resolution,
and delivery are all local and stdlib-only, and a missing or corrupt config.json
degrades the message rather than suppressing it.

WHAT THIS STILL DOES NOT CATCH: a check that never runs. If the timer stops
firing, the box is off, or systemd never starts the unit, there is no failure to
hook and this stays silent -- and silence is what healthy looks like here. That
gap needs an external deadman, which the README records as a deliberate 2026-08-12
decision against.

EXIT CODES:
  0  the alert was delivered
  1  it was not -- nothing else will tell you, so this is as loud as it gets
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import subprocess
import sys
import urllib.request

HOME = pathlib.Path.home()
HERE = pathlib.Path(__file__).resolve().parent
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def telegram_token_from_existing_store(hermes_home: pathlib.Path | None) -> str:
    """Read the existing Hermes failback token without duplicating the secret.

    The post-migration TMN host keeps its Telegram credential in
    ``llm-failback.json`` instead of an environment file. Accept that store only
    when the current user owns a regular, non-symlinked, mode-0600 file.
    """
    candidates = []
    if hermes_home:
        candidates.append(hermes_home / "llm-failback.json")
    candidates.append(HOME / ".hermes" / "llm-failback.json")
    for path in dict.fromkeys(candidates):
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                continue
            token = str((json.loads(path.read_text()).get("telegram") or {}).get(
                "bot_token"
            ) or "").strip()
            if token:
                return token
        except Exception:
            pass
    return ""


def env_val(name: str, hermes_home: pathlib.Path | None) -> str:
    """Resolve a secret from the process env, falling back to .env files.

    Deliberately a local copy of the check's resolver rather than an import. The
    order matters on hermes-tmn, where the profile .env holds the Team Nebula bot
    token and the root .env holds a different one: the profile must win.
    """
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    candidates = []
    if hermes_home:
        candidates.append(hermes_home / ".env")
    candidates += [HOME / ".hermes" / ".env", HOME / ".env"]
    for envf in candidates:
        try:
            for line in envf.read_text().splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    val = line[len(name) + 1:].strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    if val.strip():
                        return val.strip()
        except Exception:
            pass
    if name == "TELEGRAM_BOT_TOKEN":
        return telegram_token_from_existing_store(hermes_home)
    return ""


def read_config(path: pathlib.Path) -> dict:
    """Best-effort config read. A broken config must not suppress the alert."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def journal_tail(unit: str, lines: int = 15) -> str:
    """The failing unit's own output, so the alert says what actually broke."""
    if not unit:
        return ""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(lines),
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=20,
        )
        return (r.stdout or "").strip()
    except Exception as e:
        return f"(could not read journal: {type(e).__name__})"


def build_message(cfg: dict, unit: str, tail: str) -> str:
    host = cfg.get("host_label") or "unknown host"
    bot = cfg.get("bot_label") or "a Hermes bot"
    if "self-heal" in unit:
        head = (
            f"🚨 The watchdog healer on {host}: SELF-HEAL REPAIR FAILED.\n\n"
            f"Unit: {unit}\n"
            f"Watches: {bot}\n\n"
            "The healer exited non-zero after a bounded repair failed or a safety "
            "check disarmed the cycle. The healer recorded the alert edge before "
            "systemd sent this Telegram message.\n\n"
            "Check it by hand:\n"
            f"  systemctl --user status {unit}\n"
            f"  journalctl --user -u {unit} -n 50\n"
        )
    else:
        head = (
            f"🚨 The health check on {host} FAILED TO REPORT.\n\n"
            f"Unit: {unit or 'unknown'}\n"
            f"Watches: {bot}\n\n"
            "It exited non-zero, which means one of: it could not reach any of its "
            "alert channels, it was disarmed (unreadable auth store, corrupt state, "
            "unresolvable secret), or it crashed. Whatever it found, it could not "
            "tell you the normal way — so this arrived over Telegram instead.\n\n"
            "Check it by hand:\n"
            f"  systemctl --user status {unit or '<unit>'}\n"
            f"  journalctl --user -u {unit or '<unit>'} -n 50\n"
        )
    if tail:
        # Telegram rejects messages over 4096 chars; keep the tail bounded and
        # trim from the front so the most recent lines survive.
        budget = 3500 - len(head)
        body = tail[-budget:] if budget > 0 else ""
        if body:
            head += f"\nLast output:\n{body}\n"
    return head


def send_telegram(token: str, chat_id: str, text: str, thread_id: str = "") -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError("telegram error: " + json.dumps(resp)[:300])


def run(args) -> int:
    cfg_path = pathlib.Path(args.config).expanduser() if args.config else HERE / "config.json"
    cfg = read_config(cfg_path)
    if cfg.get("hermes_home"):
        hermes_home = pathlib.Path(os.path.expanduser(cfg["hermes_home"]))
    else:
        # No Hermes home: either the config is unreadable, or this is the observer
        # host, which has no auth store and keeps its .env beside the script. Look
        # there before the Hermes paths, which on that box do not exist at all.
        hermes_home = cfg_path.parent if cfg_path.parent.is_dir() else HERE

    token = env_val("TELEGRAM_BOT_TOKEN", hermes_home)
    chat = env_val("TELEGRAM_HOME_CHANNEL", hermes_home)
    if not chat:
        channels = cfg.get("channels")
        telegram = channels.get("telegram") if isinstance(channels, dict) else None
        if not isinstance(telegram, dict):
            telegram = {}
        chat = str(telegram.get("chat_id") or "").strip()
    thread = env_val("TELEGRAM_HOME_CHANNEL_THREAD_ID", hermes_home)
    unit = (os.environ.get("MONITOR_UNIT") or args.unit or "").strip()
    text = build_message(cfg, unit, journal_tail(unit, args.lines))

    if args.dry_run:
        print(f"config={cfg_path} host={cfg.get('host_label')} "
              f"token={'present' if token else 'MISSING'} "
              f"chat={'present' if chat else 'MISSING'} thread={thread or '-'}")
        print("--- TELEGRAM ---")
        print(text)
        return 0 if (token and chat) else 1

    if not token or not chat:
        print("CANNOT ESCALATE: TELEGRAM_BOT_TOKEN or TELEGRAM_HOME_CHANNEL "
              f"unresolvable (config={cfg_path}). The health check failed and "
              "nobody has been told.", file=sys.stderr)
        return 1

    try:
        send_telegram(token, chat, text, thread)
    except Exception as e:
        print(f"CANNOT ESCALATE: telegram send failed: {type(e).__name__}: {e}. "
              "The health check failed and nobody has been told.", file=sys.stderr)
        return 1

    print(f"escalated to telegram chat {chat} for unit {unit}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Escalate a failed health-check run over Telegram. Detect only; never repairs.")
    ap.add_argument("--unit", default="", help="the systemd unit that failed (systemd passes %%i)")
    ap.add_argument("--config", default=None,
                    help="health-check config.json (default: next to this script)")
    ap.add_argument("--lines", type=int, default=15, help="journal lines to include")
    ap.add_argument("--dry-run", action="store_true")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
