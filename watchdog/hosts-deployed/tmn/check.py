#!/usr/bin/env python3
"""Watch the Codex credential the Team Nebula Hermes gateway runs on.

SILENT WHEN HEALTHY. Alerts only on the transition into failure, because a check
that pings ops while everything is fine is noise they learn to ignore.

Replaces the version retired 2026-07-29, which had three defects:
  * it read ``~/.hermes/auth.json`` — the gateway runs with
    HERMES_HOME=~/.hermes/profiles/tmn and reads the PROFILE auth file, so the
    old check was watching a credential nothing used;
  * its emailed runbook omitted ``--profile tmn``, so following it re-authed the
    wrong profile and appeared to do nothing;
  * it judged health purely on access-token expiry, which misses the failure
    that actually happened — the refresh token being consumed by another box.

Detection here is structural, and deliberately does NOT trust
``hermes auth status``: on 2026-07-29 that reported ``logged in`` for a
credential with zero pooled entries and no refresh token at all. It reads
auth.json directly and trusts Hermes' own ``last_auth_error`` record, which is
written when a real refresh fails.

Alerting:
  ok   -> down : one Slack post to #tmn-ops + one email
  down -> down : quiet for RENOTIFY_S, then a single reminder
  down -> ok   : silent re-arm, no "recovered" message
  unknown      : never pages, never changes state (parse/network trouble)
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
HERMES_HOME = pathlib.Path(
    os.environ.get("HERMES_HOME", HOME / ".hermes" / "profiles" / "tmn")
)
AUTH = HERMES_HOME / "auth.json"          # the PROFILE auth file, not ~/.hermes/auth.json
CONFIG = HERMES_HOME / "config.yaml"
HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "state.json"

PROVIDER = "openai-codex"
RENOTIFY_S = 24 * 3600
# Access tokens last ~10 days and refresh automatically. Only warn about expiry
# when there is no refresh token to renew with.
OPS_CHANNEL = "C09FLJDCAJD"               # #tmn-ops
ALERT_EMAILS = ["shawn@teamnebula.ai"]
COMPOSIO_USER_ID = "user_uwgmr"
COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEND_EMAIL"

SUBJECT = "Action needed: Team Nebula Hermes lost its Codex sign-in (hermes-tmn)"

REAUTH_STEPS = """A HUMAN MUST DO THIS. It cannot be automated and nothing will
self-heal: OpenAI now mandates 2FA on sign-in, so the login has to be completed
by a person at a browser. Until someone runs these steps the bot stays down.

1. SSH to the VM:
   gcloud compute ssh hermes-tmn --project teamnebula-os --zone us-central1-a --tunnel-through-iap

2. Start the device-code login. BOTH the env var and the flag matter — without
   them the credential lands in the default profile, which the gateway never reads:
   HERMES_HOME=$HOME/.hermes/profiles/tmn hermes --profile tmn auth add openai-codex --type oauth --no-browser

3. It prints https://auth.openai.com/codex/device plus a short code. Open the URL
   in any browser, enter the code, sign in, and complete the 2FA challenge.
   Run it under tmux if your session might drop — the process must stay alive to
   receive the token, and 2FA makes this step slower than the CLI's default wait.
   If it gives up before you finish, re-run with a longer --timeout.

4. Confirm it landed (do NOT trust `auth status`, it reports "logged in" even when
   no usable credential exists):
   hermes --profile tmn auth list          # want: openai-codex (1 credentials)

5. Restart the gateway:
   systemctl --user restart hermes-gateway-tmn.service

6. Verify a real turn:
   hermes --profile tmn -z "Reply with exactly one word: PONG\""""

IMPORTANT_NOTE = """Why this needs a person: token REFRESH is automated and works
fine on its own. This alert means the refresh chain itself is broken, and the only
repair is a full re-login — which OpenAI now gates behind 2FA. There is no unattended
path back. Do not wait for it to recover by itself.

Use a FRESH authorization, not a copy of the one hostinger holds.
Both boxes previously shared one OAuth lineage; hostinger's codex-keepalive rotates
the refresh token every 30 minutes and silently invalidated this box's copy, which
is what caused the July outage. The credential `label` field is cosmetic and read
the same on both boxes — compare refresh-token fingerprints, not labels."""


def _env_val(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for envf in (HERMES_HOME / ".env", HOME / ".hermes" / ".env"):
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
    """Only meaningful while the tmn gateway is actually configured on Codex.

    Without this the check would page about a credential nothing depends on, the
    way the old one would have once tmn moved to a local model.
    """
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
    """Return (ok|down|unknown, human-readable detail)."""
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

    # Hermes records its own refresh failures here. This is the signal that
    # actually fired in July, and the one an expiry check misses entirely.
    err = prov.get("last_auth_error") or {}
    if err:
        code = str(err.get("code") or "")
        relogin = bool(err.get("relogin_required"))
        at = str(err.get("at") or "")
        last_refresh = str(prov.get("last_refresh") or "")
        # Only current if the error is NEWER than the last successful refresh —
        # otherwise it is a stale record from before a successful re-auth.
        if at and last_refresh and at > last_refresh:
            if relogin or code == "refresh_token_reused":
                return "down", f"{code or 'auth error'} at {at} (relogin_required={relogin})"

    if not refresh:
        return "down", "credential has NO refresh token — cannot renew, will fail when the access token expires"

    if exp and exp <= now:
        return "down", f"access token expired {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(exp))} and did not refresh"

    when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(exp)) if exp else "unknown"
    return "ok", f"refresh token present, access token valid to {when}"


def slack_post(text: str, token: str) -> None:
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": OPS_CHANNEL, "text": text}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError("slack error: " + str(resp.get("error")))


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


def email_body(detail: str) -> str:
    return (
        "The Team Nebula Hermes agent (@Teamnebula_bot) on the GCP VM hermes-tmn "
        "can no longer sign in to Codex, so it has stopped answering.\n\n"
        f"What the check saw: {detail}\n\n"
        "The box cannot renew this credential on its own — someone needs to sign in "
        "again.\n\n"
        + REAUTH_STEPS + "\n\n"
        + IMPORTANT_NOTE + "\n\n"
        "You will not get another message about this for 24 hours, and none at all "
        "once it is working again.\n\n"
        "-- Automated check on hermes-tmn (codex-health)."
    )


def slack_text(detail: str) -> str:
    return (
        ":rotating_light: *Codex sign-in lost — Team Nebula Hermes* (`@Teamnebula_bot`, "
        "GCP `hermes-tmn`).\n"
        f"Detected: {detail}\n"
        "The bot cannot answer until someone re-authenticates.\n\n"
        "*Fix* (needs hermes-tmn access):\n```\n" + REAUTH_STEPS + "\n```"
    )


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"status": "ok", "last_alert": 0}


def save_state(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Alert ops only when the Hermes tmn Codex sign-in breaks.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report status and show what would be sent; send nothing")
    ap.add_argument("--force-down", action="store_true",
                    help="exercise the alert path (bypasses the applicability guard)")
    ap.add_argument("--no-slack", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    if not args.force_down and not gateway_uses_codex():
        print("tmn gateway is not configured on openai-codex — check not applicable, silent")
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
            print(f"\n--- SLACK -> {OPS_CHANNEL} ---\n{slack_text(detail)}")
            print(f"\n--- EMAIL -> {ALERT_EMAILS} ---\nSubject: {SUBJECT}\n\n{email_body(detail)}")
        return

    if alert:
        sent = []
        if not args.no_slack:
            tok = _env_val("SLACK_BOT_TOKEN")
            if tok:
                try:
                    slack_post(slack_text(detail), tok)
                    sent.append("slack")
                except Exception as e:
                    print(f"slack failed: {type(e).__name__}: {e}")
        if not args.no_email:
            key = _env_val("TMN_COMPOSIO_API_KEY")
            if key:
                try:
                    send_email(ALERT_EMAILS, SUBJECT, email_body(detail), key)
                    sent.append("email")
                except Exception as e:
                    print(f"email failed: {type(e).__name__}: {e}")
        st["last_alert"] = now
        print(f"status=down ({why}) sent={sent or 'NOTHING — check creds'} | {detail}")
    else:
        print(f"status={status} action={why} | {detail}")

    st["status"] = status
    save_state(st)


if __name__ == "__main__":
    main()
