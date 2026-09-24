#!/usr/bin/env python3
"""Watch a Claude Code OAuth token. Detect and alert only.

SILENT WHEN HEALTHY. Alerts only on the transition into failure.

Sibling of ``codex_health_check.py`` and follows the same rules, but detection
is fundamentally different and that is why it is a separate script rather than
another host config:

  * codex stores structured local state (``auth.json`` with ``last_auth_error``
    and a decodable JWT), so it can be judged without touching the network;
  * a Claude Code OAuth token is an opaque ``sk-ant-oat01…`` string with no
    local metadata at all. There is nothing to inspect. The only way to know
    whether it still works is to ask Anthropic.

So this makes one real authenticated call per run. That is affordable here for
the reason the codex probe was NOT: it is a 1-token request to the cheapest
model, four times a day, against an account whose quota is not the thing being
protected. The codex probe was retired because it burned the plan quota it
existed to watch — this does not have that property.

NOTHING HERE MUTATES ANYTHING. It never refreshes, never re-logs-in, never
writes the secret. Recovery is a human running `claude setup-token`.

EXIT CODES:
  0  ran correctly (healthy, quiet, or alert fully delivered)
  1  DISARMED or delivery failed -- something needs a human. The unit's
     OnFailure= fires notify_failure.py, which escalates over Telegram
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = pathlib.Path.home()
HERE = pathlib.Path(__file__).resolve().parent
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEND_EMAIL"
SLACK_URL = "https://slack.com/api/chat.postMessage"


class Disarmed(Exception):
    """The watchdog cannot do its job. Always loud, never swallowed."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path: pathlib.Path) -> dict:
    try:
        cfg = json.loads(path.read_text())
    except Exception as e:
        raise Disarmed(f"cannot read config {path}: {type(e).__name__}: {e}")
    for key in ("label", "secret_name", "gcp_project", "probe_model"):
        if not cfg.get(key):
            raise Disarmed(f"config {path} is missing required key {key!r}")
    if not (cfg.get("channels") or {}):
        raise Disarmed(f"config {path} declares no alert channels; nothing could page")
    return cfg


def env_val(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    for envf in (HOME / ".hermes/profiles/tmn/.env", HOME / ".hermes/.env", HOME / ".env"):
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
    return ""


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def lineage(token: str) -> str:
    """Fingerprint, so two systems sharing one token is visible by inspection."""
    return hashlib.sha256(token.encode()).hexdigest()[:8] if token else "none"


def read_token(cfg: dict) -> str:
    """Fetch the token from Secret Manager. Never logged, never echoed."""
    try:
        r = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             "--secret", cfg["secret_name"], "--project", cfg["gcp_project"]],
            capture_output=True, text=True, timeout=90)
    except Exception as e:
        raise Disarmed(f"gcloud failed: {type(e).__name__}: {e}")
    if r.returncode != 0 or not r.stdout.strip():
        raise Disarmed(
            f"cannot read secret {cfg['secret_name']} from {cfg['gcp_project']}: "
            f"{(r.stderr or '').strip()[:200]}")
    return r.stdout.strip()


def detect(cfg: dict) -> tuple[str, str]:
    """ok / down / unknown, from a live authenticated call.

    Only 401/403 counts as down. A rate limit or a 5xx says nothing about the
    credential, and paging on those trains you to ignore the alert.
    """
    token = read_token(cfg)
    fp = lineage(token)

    body = json.dumps({
        "model": cfg["probe_model"],
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={"authorization": "Bearer " + token,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=45)
        return "ok", f"token accepted (HTTP {r.status}, lineage={fp})"
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode(errors="replace")[:200]
        except Exception:
            payload = ""
        if e.code in (401, 403):
            return "down", (f"Anthropic rejected the token (HTTP {e.code}, "
                            f"lineage={fp}): {payload}")
        return "unknown", f"HTTP {e.code} — not an auth verdict: {payload}"
    except Exception as e:
        return "unknown", f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------

def send_email(ch: dict, subject: str, body: str, api_key: str) -> None:
    to = list(ch["to"])
    args = {"recipient_email": to[0], "subject": subject, "body": body, "is_html": False}
    if len(to) > 1:
        args["extra_recipients"] = to[1:]
    payload = {"user_id": ch["composio_user_id"], "arguments": args}
    if ch.get("connected_account_id"):
        payload["connected_account_id"] = ch["connected_account_id"]
    req = urllib.request.Request(
        COMPOSIO_EXEC,
        data=json.dumps(payload).encode(),
        headers={"x-api-key": api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        resp = json.loads(r.read())
    if resp.get("successful") is not True:
        raise RuntimeError("composio email error: " + json.dumps(resp)[:300])


def slack_post(ch: dict, text: str, token: str) -> None:
    req = urllib.request.Request(
        SLACK_URL,
        data=json.dumps({"channel": ch["channel"], "text": text,
                         "unfurl_links": False}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError("slack error: " + json.dumps(resp)[:300])


def alert_text(cfg: dict, detail: str) -> str:
    url = cfg.get("reauth_url", "")
    link = (f"Reauth here: {url}\n"
            f"  (run step 1 below first — you need a fresh token to paste)\n\n") if url else ""
    return (
        f"{cfg['label']} can no longer authenticate to Anthropic, so its Claude-backed "
        f"features have stopped working.\n\n"
        f"What the check saw: {detail}\n\n"
        + link
        + "\n".join(cfg.get("runbook") or []) + "\n\n"
        + "\n".join(cfg.get("context_note") or []) + "\n\n"
        "You will not get another message about this for 24 hours, and none at all "
        "once it is working again.\n\n"
        f"-- Automated check (claude-health)."
    )


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"status": "ok", "last_alert": 0}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        raise Disarmed(f"state file {path} is corrupt: {type(e).__name__}: {e}")


def save_state(path: pathlib.Path, s: dict) -> None:
    try:
        path.write_text(json.dumps(s, indent=2))
    except Exception as e:
        raise Disarmed(f"cannot write state {path}: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args) -> int:
    cfg = load_config(pathlib.Path(args.config).expanduser())
    state_path = (pathlib.Path(args.state_file).expanduser()
                  if args.state_file else HERE / "state.json")
    renotify_s = int(cfg.get("renotify_s", 24 * 3600))

    print(f"target={cfg['label']} secret={cfg['gcp_project']}/{cfg['secret_name']} "
          f"state={state_path}")

    status, detail = (("down", "forced by --force-down") if args.force_down
                      else detect(cfg))

    if status == "unknown":
        print(f"status=unknown ({detail}) — no action, state untouched")
        return 0

    st = load_state(state_path)
    prev = st.get("status", "ok")
    now = int(time.time())

    alert = False
    if status == "down":
        if prev != "down":
            alert, why = True, "ok->down (first failure)"
        elif now - int(st.get("last_alert", 0)) >= renotify_s:
            alert, why = True, "still down, reminder"
        else:
            why = "still down, inside quiet window"
    else:
        why = "healthy (silent by design)" if prev == "ok" else "recovered (silent re-arm)"

    channels = cfg["channels"]

    if args.dry_run:
        print(f"status={status} prev={prev} detail={detail!r} action={why}")
        if alert:
            for name in ("slack", "email"):
                ch = channels.get(name)
                if not ch or getattr(args, f"no_{name}"):
                    continue
                where = ch["channel"] if name == "slack" else ch["to"]
                print(f"\n--- {name.upper()} -> {where} ---\n{alert_text(cfg, detail)}")
        return 0

    delivered, failures = 0, []
    if alert:
        ch = channels.get("slack")
        if ch and not args.no_slack:
            tok = env_val(ch["token_env"])
            if not tok:
                failures.append(f"slack: {ch['token_env']} unresolvable")
            else:
                try:
                    slack_post(ch, alert_text(cfg, detail), tok)
                    delivered += 1
                    print(f"slack -> {ch['channel']}")
                except Exception as e:
                    failures.append(f"slack: {type(e).__name__}: {e}")

        ch = channels.get("email")
        if ch and not args.no_email:
            key = env_val(ch["key_env"])
            if not key:
                failures.append(f"email: {ch['key_env']} unresolvable")
            else:
                try:
                    send_email(ch, cfg["subject"], alert_text(cfg, detail), key)
                    delivered += 1
                    print(f"emailed {ch['to']}")
                except Exception as e:
                    failures.append(f"email: {type(e).__name__}: {e}")

        st["last_alert"] = now
        print(f"status=down ({why}) | {detail}")
    else:
        print(f"status={status} action={why} | {detail}")

    st["status"] = status
    save_state(state_path, st)

    if alert and delivered == 0:
        for f in failures:
            print(f"  DELIVERY FAILURE: {f}", file=sys.stderr)
        print("ALERTED BUT NOTHING WAS DELIVERED — no one has been told", file=sys.stderr)
        return 1
    for f in failures:
        print(f"  partial delivery failure: {f}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Alert when a Claude Code OAuth token stops authenticating. Detect only.")
    ap.add_argument("--config", default=str(HERE / "config.json"),
                    help="per-target config (default: config.json next to this script)")
    ap.add_argument("--state-file", default=None,
                    help="override state path — use for drills so they cannot "
                         "write production state and suppress a real outage")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-down", action="store_true",
                    help="exercise the alert path (bypasses detection entirely)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-slack", action="store_true")
    args = ap.parse_args()
    try:
        return run(args)
    except Disarmed as e:
        print(f"WATCHDOG DISARMED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
