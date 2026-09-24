#!/usr/bin/env python3
"""Watch the Codex credential Shawn's personal Hermes gateway runs on (hostinger).

SILENT WHEN HEALTHY. On the transition into failure it emails Shawn and files ONE
Linear ticket assigned to him. No Slack, by request.

Sibling of the hermes-tmn check, with hostinger's differences baked in: the
default profile (no --profile flag), `hermes-gateway.service`, and the
`codex-keepalive.timer` that refreshes this box's token every 30 minutes — so a
failure here means the keepalive itself has stopped working, not merely that a
token aged out.

Detection reads auth.json directly and trusts Hermes' own `last_auth_error`.
It deliberately does NOT use `hermes auth status`: on 2026-07-29 that reported
`logged in` for a credential with zero pooled entries and no refresh token.

Alerting:
  ok   -> down : one email + one Linear ticket (assigned to Shawn)
  down -> down : quiet for RENOTIFY_S, then a single reminder email (no new ticket)
  down -> ok   : silent re-arm, no "recovered" message
  unknown      : never alerts, never changes state
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

HOME = pathlib.Path.home()
HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
AUTH = HERMES_HOME / "auth.json"
CONFIG = HERMES_HOME / "config.yaml"
HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "state.json"

PROVIDER = "openai-codex"
GATEWAY_UNIT = "hermes-gateway.service"
RENOTIFY_S = 24 * 3600

ALERT_EMAILS = ["shawn@teamnebula.ai"]
COMPOSIO_USER_ID = "user_uwgmr"
COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEND_EMAIL"

LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_TEAM_TMNO = "36388c9e-35ec-49a8-b873-3c01ad457086"          # Ops
LINEAR_ASSIGNEE_SHAWN = "27c9c520-8566-453c-8dca-bd1164e72325"     # shawn@teamnebula.ai

SUBJECT = "Action needed: personal Hermes lost its Codex sign-in (hostinger)"
TICKET_TITLE = "Codex sign-in lost — personal Hermes (hostinger)"

REAUTH_STEPS = """A HUMAN MUST DO THIS. It cannot be automated and nothing will
self-heal: OpenAI now mandates 2FA on sign-in, so the login has to be completed by
a person at a browser. Until someone runs these steps the bot stays down.

1. SSH to the box:
   ssh hostinger

2. Start the device-code login (default profile — no --profile flag here):
   hermes auth add openai-codex --type oauth --no-browser

3. It prints https://auth.openai.com/codex/device plus a short code. Open the URL
   in any browser, enter the code, sign in, and complete the 2FA challenge.
   Run it under tmux if your session might drop — the process must stay alive to
   receive the token, and 2FA makes this step slower than the CLI's default wait.
   If it gives up before you finish, re-run with a longer --timeout.

4. Confirm it landed (do NOT trust `auth status`, it reports "logged in" even when
   no usable credential exists):
   hermes auth list          # want: openai-codex (1 credentials)

5. Restart the gateway:
   systemctl --user restart hermes-gateway.service

6. Verify a real turn:
   hermes -z "Reply with exactly one word: PONG\""""

CONTEXT_NOTE = """Why this needs a person: token REFRESH is automated and works fine
on its own — codex-keepalive.timer probes every 30 minutes and refreshes only when
needed. This alert means the refresh chain itself is broken, and the only repair is a
full re-login, which OpenAI now gates behind 2FA. There is no unattended path back;
codex_watchdog.py runs WATCHDOG_MONITOR_ONLY=1 and will not attempt one. Do not wait
for it to recover by itself.

Worth checking `journalctl --user -u codex-keepalive.service` first — a failure here
usually means the keepalive broke rather than a token simply ageing out.

Use a FRESH authorization, not a copy of the one hermes-tmn holds. The two boxes
sharing one OAuth lineage is what caused the July outage: whichever box refreshes
last silently invalidates the other. The credential `label` field is cosmetic —
compare refresh-token fingerprints, not labels."""


def _env_val(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for envf in (HERMES_HOME / ".env", HOME / ".env"):
        try:
            for line in envf.read_text().splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    val = line[len(name) + 1:].strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    return val.strip()
        except Exception:
            pass
    return ""


def gateway_uses_codex() -> bool:
    import re
    try:
        text = CONFIG.read_text()
    except Exception:
        return False
    m = re.search(r"^model:\s*$", text, re.M)
    if not m:
        return False
    block = text[m.end():]
    stop = re.search(r"^[^\s-]", block, re.M)
    block = block[: stop.start()] if stop else block
    p = re.search(r"^\s+provider:\s*(\S+)", block, re.M)
    return (p.group(1).strip().strip("'\"").lower() if p else "") == PROVIDER


def _exp_of(access_token: str):
    if not access_token or access_token.count(".") != 2:
        return None
    p = access_token.split(".")[1]
    p += "=" * (-len(p) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p)).get("exp")
    except Exception:
        return None


def detect() -> tuple[str, str]:
    try:
        d = json.loads(AUTH.read_text())
    except Exception as e:
        return "unknown", f"cannot read {AUTH}: {type(e).__name__}"

    prov = (d.get("providers") or {}).get(PROVIDER) or {}
    pool = (d.get("credential_pool") or {}).get(PROVIDER) or []
    if not prov and not pool:
        return "down", "no Codex credential at all (providers block empty, pool empty)"

    toks = prov.get("tokens") or {}
    refresh = toks.get("refresh_token") or ""
    access = toks.get("access_token") or ""
    exp = _exp_of(access)
    now = time.time()

    err = prov.get("last_auth_error") or {}
    if err:
        code = str(err.get("code") or "")
        relogin = bool(err.get("relogin_required"))
        at = str(err.get("at") or "")
        last_refresh = str(prov.get("last_refresh") or "")
        if at and last_refresh and at > last_refresh:
            if relogin or code == "refresh_token_reused":
                return "down", f"{code or 'auth error'} at {at} (relogin_required={relogin})"

    if not refresh:
        return "down", "credential has NO refresh token — cannot renew; the keepalive will fail"

    if exp and exp <= now:
        return "down", (f"access token expired {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(exp))} "
                        f"and codex-keepalive did not refresh it")

    when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(exp)) if exp else "unknown"
    return "ok", f"refresh token present, access token valid to {when}"


def send_email(recipients, subject, body, api_key) -> None:
    to = list(recipients)
    args = {"recipient_email": to[0], "subject": subject, "body": body, "is_html": False}
    if len(to) > 1:
        args["extra_recipients"] = to[1:]
    req = urllib.request.Request(
        COMPOSIO_EXEC,
        data=json.dumps({"user_id": COMPOSIO_USER_ID, "arguments": args}).encode(),
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        resp = json.loads(r.read())
    if not ((resp.get("successful") is True) or (resp.get("data") and not resp.get("error"))):
        raise RuntimeError("composio email error: " + json.dumps(resp)[:400])


def linear_create(detail: str, api_key: str) -> str:
    """File ONE ticket assigned to Shawn. Returns the issue URL."""
    description = (
        f"The personal Hermes gateway on `hostinger` can no longer sign in to Codex, "
        f"so `@Screddy_bot` has stopped answering.\n\n"
        f"**What the check saw:** {detail}\n\n"
        f"### Fix\n\n```\n{REAUTH_STEPS}\n```\n\n### Context\n\n{CONTEXT_NOTE}\n\n"
        f"_Auto-filed by the hostinger codex-health check._"
    )
    query = """
    mutation IssueCreate($teamId: String!, $title: String!, $description: String!, $assigneeId: String!) {
      issueCreate(input: {teamId: $teamId, title: $title, description: $description, assigneeId: $assigneeId}) {
        success
        issue { id identifier url }
      }
    }"""
    body = json.dumps({
        "query": query,
        "variables": {
            "teamId": LINEAR_TEAM_TMNO,
            "title": TICKET_TITLE,
            "description": description,
            "assigneeId": LINEAR_ASSIGNEE_SHAWN,
        },
    }).encode()
    req = urllib.request.Request(
        LINEAR_URL, data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        resp = json.load(r)
    if resp.get("errors"):
        raise RuntimeError("linear error: " + json.dumps(resp["errors"])[:300])
    issue = (((resp.get("data") or {}).get("issueCreate") or {}).get("issue")) or {}
    return issue.get("url") or issue.get("identifier") or "(created)"


def email_body(detail: str, ticket: str | None) -> str:
    return (
        "Your personal Hermes agent (@Screddy_bot) on hostinger can no longer sign in "
        "to Codex, so it has stopped answering.\n\n"
        f"What the check saw: {detail}\n\n"
        + (f"Linear ticket: {ticket}\n\n" if ticket else "")
        + REAUTH_STEPS + "\n\n"
        + CONTEXT_NOTE + "\n\n"
        "You will not get another message about this for 24 hours, and none at all "
        "once it is working again.\n\n"
        "-- Automated check on hostinger (codex-health)."
    )


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"status": "ok", "last_alert": 0, "ticket_url": None}


def save_state(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Alert Shawn only when hostinger's Codex sign-in breaks.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-down", action="store_true",
                    help="exercise the alert path (bypasses the applicability guard)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-linear", action="store_true")
    args = ap.parse_args()

    if not args.force_down and not gateway_uses_codex():
        print("hostinger gateway is not configured on openai-codex — not applicable, silent")
        return

    status, detail = ("down", "forced by --force-down") if args.force_down else detect()

    if status == "unknown":
        print(f"status=unknown ({detail}) — no action, state untouched")
        return

    st = load_state()
    prev = st.get("status", "ok")
    now = int(time.time())

    alert = False
    if status == "down":
        if prev != "down":
            alert, why = True, "ok->down (first failure)"
        elif now - int(st.get("last_alert", 0)) >= RENOTIFY_S:
            alert, why = True, "still down, 24h reminder"
        else:
            why = "still down, inside quiet window"
    else:
        why = "healthy (silent by design)" if prev == "ok" else "recovered (silent re-arm)"

    if args.dry_run:
        print(f"status={status} prev={prev} detail={detail!r} action={why}")
        if alert:
            print(f"\n--- EMAIL -> {ALERT_EMAILS} ---\nSubject: {SUBJECT}\n\n{email_body(detail, 'https://linear.app/...')}")
            print(f"\n--- LINEAR -> team TMNO, assignee Shawn ---\n{TICKET_TITLE}")
        return

    if alert:
        ticket = st.get("ticket_url")
        # One ticket per outage: only file on the first failure, not the reminder.
        if not args.no_linear and prev != "down":
            key = _env_val("TMN_LINEAR_API_KEY")
            if key:
                try:
                    ticket = linear_create(detail, key)
                    st["ticket_url"] = ticket
                    print(f"linear ticket: {ticket}")
                except Exception as e:
                    print(f"linear failed: {type(e).__name__}: {e}")
        if not args.no_email:
            key = _env_val("TMN_COMPOSIO_API_KEY")
            if key:
                try:
                    send_email(ALERT_EMAILS, SUBJECT, email_body(detail, ticket), key)
                    print(f"emailed {ALERT_EMAILS}")
                except Exception as e:
                    print(f"email failed: {type(e).__name__}: {e}")
        st["last_alert"] = now
        print(f"status=down ({why}) | {detail}")
    else:
        if status == "ok":
            st["ticket_url"] = None      # re-arm so the next outage files a fresh ticket
        print(f"status={status} action={why} | {detail}")

    st["status"] = status
    save_state(st)


if __name__ == "__main__":
    main()
