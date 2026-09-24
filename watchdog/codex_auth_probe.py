#!/usr/bin/env python3
"""Read-only live probe of a Hermes codex credential. OPERATOR TOOL — run by hand.

This makes a real call to OpenAI using the CURRENT access token. It never
refreshes and never writes, so it cannot rotate the single-use refresh token and
cannot race a running gateway.

NOT on any timer, deliberately. It used to run every 30 minutes as part of the
retired self-heal, and the keepalive log shows what that cost: over ~7 weeks it
produced 3 genuine BROKEN results against 209 UNKNOWNs, and 209 of those were
HTTP 429 usage_limit_reached. It was consuming the plan quota it existed to
protect and then classifying its own exhaustion as "transient" — including one
continuous 4.6-hour blind window. The scheduled watchdog (codex_health_check.py)
reads local signals instead, which caught all three real breakages for free.

Reach for this during triage, when you want to know whether OpenAI still accepts
the credential right now rather than what the local files claim.

  exit 0  OK      the token works
  exit 1  BROKEN  rejected — a human must re-login (2FA; nothing self-heals)
  exit 2  UNKNOWN transient: 5xx, network, or unparseable local state
  exit 3  QUOTA   429 usage limit — auth is FINE, the plan is exhausted

QUOTA is separate from UNKNOWN on purpose. Collapsing them hid the fact that the
old probe spent hours per week blind, because "quota exhausted" and "everything
is fine" produced identical output.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

from auth_state import selected_codex_credential

ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"


def resolve_auth(args) -> pathlib.Path:
    if args.auth_json:
        return pathlib.Path(args.auth_json).expanduser()
    if args.config:
        cfg = json.loads(pathlib.Path(args.config).expanduser().read_text())
        if not cfg.get("hermes_home"):
            raise SystemExit("config has no 'hermes_home'; refusing to guess an auth store")
        return pathlib.Path(os.path.expanduser(cfg["hermes_home"])) / "auth.json"
    raise SystemExit("need --auth-json or --config")


def resolve_credential(auth_path: pathlib.Path) -> tuple[str, str]:
    auth = json.loads(auth_path.read_text())
    selected = selected_codex_credential(auth)
    if not selected or not selected.get("access_token"):
        raise ValueError("no probeable Codex credential")
    return str(selected["access_token"]), str(selected.get("label") or "unknown")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--auth-json", help="path to the Hermes auth.json to probe")
    ap.add_argument("--config", help="host config to read hermes_home from")
    ap.add_argument("--model", default="gpt-5.5",
                    help="model to probe with (default gpt-5.5; must be one the "
                         "account can actually use — a ChatGPT-plan account "
                         "rejects every *-codex variant)")
    args = ap.parse_args()

    auth_path = resolve_auth(args)
    label = "unknown"
    try:
        at, label = resolve_credential(auth_path)
    except Exception as e:
        print(f"UNKNOWN: cannot read {auth_path} (credential={label}; "
              f"{type(e).__name__}: {e})")
        return 2
    try:
        p = at.split(".")[1]
        p += "=" * (-len(p) % 4)
        claims = json.loads(base64.urlsafe_b64decode(p))
        acct = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    except Exception as e:
        print(f"UNKNOWN: cannot parse account_id (credential={label}; "
              f"{type(e).__name__}: {e})")
        return 2

    body = json.dumps({
        "model": args.model, "instructions": "probe",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "Reply with exactly: OK"}]}],
        "stream": True, "store": False,
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                 "chatgpt-account-id": acct, "OpenAI-Beta": "responses=experimental",
                 "originator": "codex_cli_rs", "User-Agent": "codex-auth-probe"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        print(f"OK: {r.status} (credential={label}, auth={auth_path}, model={args.model})")
        return 0
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode(errors="replace")
        except Exception:
            payload = ""
        if e.code == 429 and "usage_limit" in payload.lower():
            print(f"QUOTA: {e.code} (credential={label}) {payload[:200]}")
            return 3
        # Any 401/403 is BROKEN. The version this replaces also required the body
        # to match a substring allowlist, so a reworded OpenAI error fell through
        # to UNKNOWN and an outage went unreported. That bias made sense when
        # BROKEN triggered a destructive headless reauth; now that the only
        # consequence is telling a human, a false page costs nothing and a
        # swallowed 401 costs an outage.
        if e.code in (401, 403):
            print(f"BROKEN: {e.code} (credential={label}) {payload[:200]}")
            return 1
        print(f"UNKNOWN: {e.code} (credential={label}) {payload[:200]}")
        return 2
    except Exception as e:
        print(f"UNKNOWN: {type(e).__name__} (credential={label}): {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
