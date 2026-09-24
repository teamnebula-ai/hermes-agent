#!/usr/bin/env bash
# codex-keepalive.sh — probe-first, self-healing Hermes codex auth keeper.
#
# WHY: Hermes' codex (ChatGPT-plan) OAuth refresh token is SINGLE-USE and rotates on
# every refresh. Two processes refreshing the same token (the gateway + a separate
# keepalive warmup) race and trip OpenAI's `refresh_token_reused`, which invalidates the
# whole token family and forces a re-login. The old keepalive *unconditionally* ran a
# refreshing `hermes -z` every 30 min, which was itself a frequent racer.
#
# NEW DESIGN (reduces races + self-heals, no human):
#   1. flock — only one scheduled refresh/reauth runs at a time.
#   2. PROBE (read-only, no refresh) — if the access token still works, do nothing
#      (this is the common case, and it never touches the refresh token → no race).
#   3. If the probe is BROKEN, try ONE refresh (`hermes -z`) and re-probe — covers the
#      "access token merely expired, refresh token still good" case.
#   4. If still broken, the refresh token itself is dead → run the headless device-code
#      reauth (run_restore.sh, fully server-side, no Mac/human) and re-probe.
#   5. Only if reauth ALSO fails do we edge-alert Slack (ok->fail transition only).
#
# Follows the fleet health-check pattern: self-heal first, edge-triggered alert only.
set -uo pipefail

REPO="${REPO:-/home/ubuntu/headless-oauth-recovery}"
HERMES_BIN="${HERMES_BIN:-/home/ubuntu/.local/bin/hermes}"
HERMES_MODEL="${HERMES_MODEL:-openai-codex/gpt-5.4}"
SHARED_ENV="${SHARED_ENV:-/home/ubuntu/secrets/shared.env}"
STATE_DIR="${STATE_DIR:-/home/ubuntu/.openclaw/codex-keepalive}"
STATE_FILE="$STATE_DIR/last_state"
LOG_FILE="$STATE_DIR/keepalive.log"
LOCK_FILE="$STATE_DIR/refresh.lock"
PROBE="${PROBE:-$REPO/codex_auth_probe.py}"
REAUTH_SH="${REAUTH_SH:-/home/ubuntu/run_restore.sh}"
REAUTH_TIMEOUT="${REAUTH_TIMEOUT:-200}"
WARMUP_TIMEOUT="${WARMUP_TIMEOUT:-120}"
SLACK_DM_CHANNEL="${SLACK_DM_CHANNEL:-D0AGFSC9PHN}"

mkdir -p "$STATE_DIR"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$LOG_FILE"; }

# --- serialize: never run while another refresh/reauth is in flight ---
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "skip: another codex refresh/reauth holds the lock"
  exit 0
fi

probe() { python3 "$PROBE" >>"$LOG_FILE" 2>&1; return $?; }

set_state() { echo "$1" >"$STATE_FILE"; }
prev_state="$(cat "$STATE_FILE" 2>/dev/null || echo unknown)"

# --- step 1: read-only probe (no refresh, no race) ---
probe; pexit=$?
if [ "$pexit" -eq 0 ]; then
  set_state ok
  exit 0
fi
if [ "$pexit" -eq 2 ]; then
  # transient/network/5xx — do not refresh or reauth, do not flip state
  log "probe UNKNOWN (transient) — leaving state=$prev_state, no action"
  exit 0
fi

# pexit == 1: definitive auth failure
log "probe BROKEN — attempting refresh via hermes warmup"

# --- step 2: try a single refresh (access token may have just expired) ---
HYPERSWARM_MEMORY_DISABLE=1 timeout "$WARMUP_TIMEOUT" "$HERMES_BIN" -m "$HERMES_MODEL" -z "Reply with exactly: OK" >/dev/null 2>>"$LOG_FILE"
probe; pexit=$?
if [ "$pexit" -eq 0 ]; then
  log "recovered via refresh (access token was stale; refresh token healthy)"
  set_state ok
  exit 0
fi

# --- step 3: refresh token is dead — headless self-heal reauth (server-side, no human) ---
log "refresh did not recover — running headless device-code reauth"
pkill -f restore_reauth 2>/dev/null
pkill -f "auth add openai-codex" 2>/dev/null
pkill -f google-chrome 2>/dev/null
pkill Xvfb 2>/dev/null
sleep 1
timeout "$REAUTH_TIMEOUT" bash "$REAUTH_SH" >>"$LOG_FILE" 2>&1
reauth_rc=$?
log "headless reauth exit=$reauth_rc"

probe; pexit=$?
if [ "$pexit" -eq 0 ]; then
  log "SELF-HEALED via headless reauth"
  # clear the stale last_auth_error so monitoring stops flagging relogin_required
  python3 - <<'PY' >>"$LOG_FILE" 2>&1 || true
import json, os, tempfile
f = "/home/ubuntu/.hermes/auth.json"
d = json.load(open(f))
prov = d.get("providers", {}).get("openai-codex", {})
if prov.pop("last_auth_error", None) is not None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
    with os.fdopen(fd, "w") as fh: json.dump(d, fh, indent=2)
    os.replace(tmp, f); print("cleared stale last_auth_error")
PY
  set_state ok
  # recovery is silent unless we were previously failing (edge-trigger handled below)
  exit 0
fi

# --- step 4: still broken after reauth — truly needs a human; edge-alert only ---
log "STILL BROKEN after refresh + headless reauth (reauth_rc=$reauth_rc, probe=$pexit)"
set_state fail
if [ "$prev_state" = "ok" ] || [ "$prev_state" = "unknown" ]; then
  [ -f "$SHARED_ENV" ] && set -a && . "$SHARED_ENV" && set +a
  if [ -n "${SLACK_BOT_TOKEN:-}" ]; then
    msg="codex auth DOWN on neb-brain-hostinger and headless self-heal FAILED (reauth_rc=$reauth_rc). The OpenAI browser session likely expired. Run a manual hermes codex re-login on the box. Hermes model calls will fail until then."
    python3 - "$SLACK_DM_CHANNEL" "$msg" <<'PY' >>"$LOG_FILE" 2>&1 || true
import json,os,sys,urllib.request
ch,text=sys.argv[1],sys.argv[2]
tok=os.environ.get("SLACK_BOT_TOKEN","")
req=urllib.request.Request("https://slack.com/api/chat.postMessage",
    data=json.dumps({"channel":ch,"text":text}).encode(),
    headers={"Authorization":"Bearer "+tok,"Content-type":"application/json; charset=utf-8"})
print("slack:",urllib.request.urlopen(req,timeout=20).read().decode()[:200])
PY
  fi
fi
exit 1
