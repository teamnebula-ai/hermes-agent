#!/usr/bin/env python3
"""Read-only Hermes codex auth probe — definitive, no token rotation.

Uses the CURRENT access token (no refresh) against the codex responses endpoint:
  exit 0 + "OK"      -> auth works
  exit 1 + "BROKEN"  -> token invalidated / unauthorized (needs reauth)
  exit 2 + "UNKNOWN" -> transient/network/5xx (do NOT reauth on this)

The exit code lets a self-heal wrapper trigger headless reauth ONLY on a
definitive auth failure, never on a transient blip.
"""
import json, base64, sys, urllib.request, urllib.error

AUTH_JSON = "/home/ubuntu/.hermes/auth.json"

def main():
    try:
        tok = json.load(open(AUTH_JSON))["providers"]["openai-codex"]["tokens"]
        at = tok["access_token"]
    except Exception as e:
        print(f"UNKNOWN: cannot read auth.json ({e})"); return 2
    # account id lives in the JWT auth claim
    try:
        p = at.split(".")[1]; p += "=" * (-len(p) % 4)
        claims = json.loads(base64.urlsafe_b64decode(p))
        acct = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    except Exception as e:
        print(f"UNKNOWN: cannot parse account_id ({e})"); return 2
    body = json.dumps({
        "model": "gpt-5.4", "instructions": "probe",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "Reply with exactly: OK"}]}],
        "stream": True, "store": False,
    }).encode()
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/responses", data=body, method="POST",
        headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                 "chatgpt-account-id": acct, "OpenAI-Beta": "responses=experimental",
                 "originator": "codex_cli_rs", "User-Agent": "codex-auth-probe"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        print(f"OK: {r.status}"); return 0
    except urllib.error.HTTPError as e:
        payload = ""
        try: payload = e.read().decode(errors="replace")
        except Exception: pass
        low = payload.lower()
        if e.code in (401, 403) and any(s in low for s in
                ("token_invalidated", "invalidated", "invalid_request_error",
                 "expired", "unauthorized", "invalid_grant", "re-authenticate", "sign in")):
            print(f"BROKEN: {e.code} {payload[:200]}"); return 1
        # 429/5xx/other = transient — don't reauth
        print(f"UNKNOWN: {e.code} {payload[:200]}"); return 2
    except Exception as e:
        print(f"UNKNOWN: {e}"); return 2

if __name__ == "__main__":
    sys.exit(main())
