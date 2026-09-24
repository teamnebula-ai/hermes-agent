#!/usr/bin/env python3
"""Watch the Codex credential a Hermes gateway runs on. Detect and alert only.

SILENT WHEN HEALTHY. Alerts only on the transition into failure, because a check
that pings you while everything is fine is noise you learn to ignore.

This is the canonical version, shared by every host. Everything host-specific --
which auth store to read, which gateway unit, which channels to alert, and the
runbook prose -- lives in ``config.json`` next to this file. See ``hosts/``.

NOTHING HERE MUTATES ANYTHING. It never refreshes a token, never launches a
browser, never writes auth.json. OpenAI now mandates 2FA on sign-in, so the old
headless device-code self-heal cannot work by construction: recovery is a human
at a browser, and the only useful thing software can do is notice quickly and
say so clearly.

Detection deliberately does NOT trust ``hermes auth status``: on 2026-07-29 that
reported ``logged in`` for a credential with zero pooled entries and no refresh
token at all. It reads auth.json directly and trusts Hermes' own
``last_auth_error`` record, which is written when a real refresh fails.

It reports TWO kinds of failure, because they need opposite responses:

``down``   the credential cannot sign in. Fix: a human completes a device-code
           login.
``quota``  the credential signs in perfectly and the plan is out of quota. A
           re-login CANNOT fix this and telling someone to try one wastes their
           time -- during the 2026-08-15..17 outage two device-code logins were
           completed against an exhausted plan before anyone read the 429 body.

Quota is read PASSIVELY, from the ``credential_pool`` entries Hermes itself
writes (``last_status: exhausted``, ``last_error_code: 429``, and the 429 body
carrying ``resets_at``). It costs no network call and no quota. The live probe is
NOT used here and must not be put on a timer: under the old design it ran every
30 minutes and consumed the very quota it existed to protect.

Alerting:
  ok    -> failure            : one alert on every configured channel
  down/quota/sibling -> same  : quiet for renotify_s, then one reminder (no new ticket)
  peer  -> peer               : quiet until recovery; no scheduled reminders
  down  -> quota              : alerts again -- a different problem needing a different fix
  fail  -> ok                 : silent re-arm, no "recovered" message
  unknown                     : never pages, never changes state (parse/network trouble)

EXIT CODES -- a watchdog that fails silently is worse than no watchdog, so every
way this can stop working is loud:
  0  ran correctly (healthy, quiet, or alert fully delivered)
  1  DISARMED or delivery failed -- something needs a human. The unit's
     OnFailure= fires notify_failure.py, which escalates over Telegram. That
     directive was described here from the start and was not actually present in
     any unit until 2026-08-17, so for months this exit code reached stderr and
     stopped there.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

from auth_state import (
    PROVIDER,
    QUOTA_STALE_S,
    entry_quota_blocked,
    quota_blocked,
    renewable_pool_entries,
)

HOME = pathlib.Path.home()
HERE = pathlib.Path(__file__).resolve().parent
COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEND_EMAIL"
LINEAR_URL = "https://api.linear.app/graphql"
SLACK_URL = "https://slack.com/api/chat.postMessage"


class Disarmed(Exception):
    """The watchdog cannot do its job. Always loud, never swallowed.

    Every ``except`` that used to return a benign value and exit 0 raises this
    instead. A monitoring system reporting success while blind is the single
    worst outcome available to it.
    """


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path: pathlib.Path) -> dict:
    """Load per-host config. Any problem is fatal and loud.

    ``hermes_home`` intentionally has NO default. The two hosts this replaced
    disagreed on it (``~/.hermes`` vs ``~/.hermes/profiles/tmn``), and a shared
    default would silently point the TMN box at its stale root auth.json and
    report a confident, wrong "ok" -- exactly the defect fixed on 2026-07-29.
    A missing value must stop the run, not pick a plausible path.
    """
    try:
        cfg = json.loads(path.read_text())
    except Exception as e:
        raise Disarmed(f"cannot read config {path}: {type(e).__name__}: {e}")

    # An observer watches other boxes and has no local credential of its own, so
    # hermes_home and gateway_unit do not apply to it. This is NOT a default for
    # hermes_home: every host that actually checks a credential must still name
    # its auth store, because the wrong store yields a confident false "ok".
    observer = cfg.get("mode") == "observer"
    if observer:
        peers = cfg.get("peers")
        if not peers:
            raise Disarmed(f"config {path} is an observer but watches no peers")
        if not cfg.get("host_label"):
            raise Disarmed(f"config {path} is missing required key 'host_label'")
        labels = [peer.get("label") for peer in peers if isinstance(peer, dict)]
        if len(labels) != len(peers) or any(not label for label in labels):
            raise Disarmed(f"config {path} has an observer peer without a label")
        if len(set(labels)) != len(labels):
            raise Disarmed(f"config {path} has duplicate observer peer labels")
        known = set(labels)
        for peer in peers:
            covered_by = peer.get("covered_by")
            if covered_by is None:
                continue
            if not isinstance(covered_by, list) or any(
                not isinstance(label, str) or not label for label in covered_by
            ):
                raise Disarmed(
                    f"config {path} peer {peer['label']!r} has malformed covered_by"
                )
            unknown = set(covered_by) - known
            if unknown or peer["label"] in covered_by:
                raise Disarmed(
                    f"config {path} peer {peer['label']!r} has unknown or self "
                    "covered_by reference"
                )
    else:
        if not cfg.get("hermes_home"):
            raise Disarmed(
                f"config {path} has no 'hermes_home'. Refusing to guess: the wrong "
                f"auth store yields a confident false 'ok'."
            )
        for key in ("host_label", "gateway_unit"):
            if not cfg.get(key):
                raise Disarmed(f"config {path} is missing required key {key!r}")
    if not (cfg.get("channels") or {}):
        raise Disarmed(f"config {path} declares no alert channels; nothing could page")

    return cfg


def env_val(name: str, hermes_home: pathlib.Path) -> str:
    """Resolve a secret from the process env, falling back to .env files.

    The fallback is load-bearing on the TMN box: its gateway unit loads only the
    profile .env, which does not carry SLACK_BOT_TOKEN -- that lives in the root
    ~/.hermes/.env.
    """
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    for envf in (hermes_home / ".env", HOME / ".hermes" / ".env", HOME / ".env"):
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


def telegram_token_from_existing_store(hermes_home: pathlib.Path) -> str:
    """Read Hermes' existing Telegram token only from an owner-private store."""
    candidates = (hermes_home / "llm-failback.json", HOME / ".hermes" / "llm-failback.json")
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


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def gateway_uses_codex(config_yaml: pathlib.Path) -> bool:
    """True when the gateway is actually configured on codex.

    Distinguishes "config unreadable" (Disarmed -- we cannot tell, so be loud)
    from "readable, but the gateway legitimately moved off codex" (False --
    genuinely not applicable, stay quiet). The version this replaces collapsed
    both into False, so a YAML typo silently disarmed the watchdog.
    """
    import re
    try:
        text = config_yaml.read_text()
    except Exception as e:
        raise Disarmed(f"cannot read {config_yaml}: {type(e).__name__}: {e}")
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


def lineage(refresh_token: str) -> str:
    """Short fingerprint of the refresh token.

    Two hosts holding the same credential is what took both boxes down in July:
    the refresh token is single-use and rotates, so a shared lineage means each
    refresh invalidates the other host. Printing it in every alert makes that
    visible by inspection instead of requiring an incident to discover.
    """
    if not refresh_token:
        return "none"
    return hashlib.sha256(refresh_token.encode()).hexdigest()[:8]


def plan_of(access_token: str) -> str:
    """The ChatGPT plan the credential is attached to, per its own JWT claims.

    Context only, never the trigger. The claim is baked at token issuance, so it
    lags a plan change until the next refresh -- on 2026-08-17 it still read
    ``free`` for minutes after the account was upgraded and the API was already
    serving. Alerting on it would have paged through a working bot.
    """
    if not access_token or access_token.count(".") != 2:
        return ""
    p = access_token.split(".")[1]
    p += "=" * (-len(p) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return ""
    return str((claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or "")


def write_heartbeat(path: pathlib.Path, cfg: dict, status: str) -> None:
    """Record that this check ran, for the peer box to read.

    Written on every completed run, including failing ones: the peer is watching
    whether the check RUNS, which is a different question from what it found. A
    box that is correctly reporting a broken credential is alive.

    Never fatal. A heartbeat that cannot be written is worth a line on stderr, not
    a dead watchdog -- the local verdict is the more important product.
    """
    try:
        path.write_text(json.dumps({
            "host": cfg.get("host_label", ""),
            "unit": cfg.get("gateway_unit", ""),
            "status": status,
            "at": int(time.time()),
        }))
    except Exception as e:
        print(f"  heartbeat not written ({type(e).__name__}: {e})", file=sys.stderr)


def read_peer(cfg: dict, prev_fails: int) -> tuple[str, str, int]:
    """Has the peer box's watchdog run recently?

    Returns (verdict, detail, consecutive_failures) where verdict is one of
    ok / peer / unknown.

    Two failure shapes, treated differently on purpose:

    * The heartbeat is READABLE but old -- the peer box is up and something
      stopped its check. That is unambiguous, so it alerts on the first sighting.
    * The heartbeat is UNREACHABLE -- could be the peer being down, could be a
      DERP relay hiccup on a tailnet that already routes these two boxes
      indirectly. One miss is not evidence, so it takes two consecutive runs
      (~12h at the 6h cadence) before it pages.
    """
    peer = cfg.get("peer") or {}
    if not peer.get("url"):
        return "ok", "", 0

    label = peer.get("label") or "peer"
    stale_after = int(peer.get("stale_after_s", 46800))    # 13h: two 6h runs + grace
    try:
        with urllib.request.urlopen(peer["url"], timeout=20) as r:
            hb = json.loads(r.read())
    except Exception as e:
        fails = prev_fails + 1
        msg = (f"{label} heartbeat unreachable ({type(e).__name__}), "
               f"{fails} consecutive")
        if fails >= 2:
            return "peer", (f"{msg} — the peer box is unreachable or its watchdog "
                            f"is gone, so nothing is watching {peer.get('bot_label', label)}"), fails
        return "unknown", msg, fails

    age = int(time.time()) - int(hb.get("at") or 0)
    if age > stale_after:
        hrs = age / 3600.0
        return "peer", (f"{label} last ran {hrs:.1f}h ago (limit "
                        f"{stale_after / 3600:.0f}h) — its watchdog stopped running, "
                        f"so nothing is watching {peer.get('bot_label', label)}"), 0
    return "ok", f"{label} heartbeat {age // 60}m old", 0


def sibling_timers_ok(cfg: dict) -> tuple[str, str]:
    """Are this box's OTHER health timers still armed?

    A box runs more than one check. hermes-tmn runs the codex check and the NEBOS
    v2 Claude check. If the codex timer is disabled the heartbeat goes stale and
    the peer reports it -- but the Claude timer has no such shadow, so disabling
    it is invisible while the box stays healthy. That is the 2026-08-04 failure
    mode (a timer switched off, six days unnoticed) surviving in a corner.

    Local and cheap: systemd already knows. No network, no heartbeat, no extra
    moving part to rot.
    """
    timers = cfg.get("sibling_timers") or []
    if not timers:
        return "ok", ""

    stale_s = int(cfg.get("sibling_stale_s", 26 * 3600))   # 4x/day + generous slack
    now = time.time()
    broken, seen = [], []

    for timer in timers:
        try:
            enabled_r = subprocess.run(["systemctl", "--user", "is-enabled", timer],
                                       capture_output=True, text=True, timeout=20)
            next_r = subprocess.run(["systemctl", "--user", "show", timer,
                                     "-p", "NextElapseUSecRealtime", "--value"],
                                    capture_output=True, text=True, timeout=20)
            last_r = subprocess.run(["systemctl", "--user", "show", timer,
                                     "-p", "LastTriggerUSec", "--value"],
                                    capture_output=True, text=True, timeout=20)
            if any(getattr(r, "returncode", 0) != 0 for r in (enabled_r, next_r, last_r)):
                raise OSError("systemctl returned nonzero")
            enabled = enabled_r.stdout.strip()
            nxt = next_r.stdout.strip()
            last = last_r.stdout.strip()
        except Exception as e:
            # Cannot tell is not the same as broken; stay quiet rather than page
            # on a systemctl hiccup.
            seen.append(f"{timer}: uncheckable ({type(e).__name__})")
            continue

        if enabled != "enabled":
            broken.append(f"{timer} is {enabled or 'missing'}")
        elif not nxt:
            broken.append(f"{timer} is enabled but has no next run scheduled")
        else:
            # A never-triggered timer is a fresh install, not a fault.
            if last and last not in ("n/a", "0"):
                try:
                    age = now - time.mktime(time.strptime(last[:24], "%a %Y-%m-%d %H:%M:%S"))
                    if age > stale_s:
                        broken.append(f"{timer} last ran {age / 3600:.1f}h ago")
                        continue
                except Exception:
                    pass
            seen.append(f"{timer}: armed")

    if broken:
        return "sibling", ("another watchdog on this box has stopped: " + "; ".join(broken) +
                           ". Its target is unmonitored — this box is otherwise fine.")
    return "ok", "; ".join(seen)


def read_peers(cfg: dict, fails: dict) -> tuple[str, str, dict]:
    """Watch one or more peers. ANY dark peer is reported.

    The observer applies declared coverage (see observe_peers): it suppresses a
    dark peer only when that peer names another watched peer whose heartbeat is
    fresh. Here each peer is something nothing else watches, so each one counts.

    Accepts the older single ``peer`` object as well, so a host config that
    predates the list keeps working.
    """
    peers = cfg.get("peers")
    if not peers:
        single = cfg.get("peer")
        peers = [single] if single else []
    if not peers:
        return "ok", "", {}

    out, dark, details = {}, [], []
    for peer in peers:
        label = peer.get("label") or peer.get("url", "peer")
        status, detail, n = read_peer({"peer": peer}, int(fails.get(label, 0)))
        out[label] = n
        details.append(detail or status)
        if status == "peer":
            dark.append(detail)

    if dark:
        return "peer", "; ".join(dark), out
    return "ok", "; ".join(d for d in details if d), out


def observe_peers(cfg: dict, fails: dict) -> tuple[str, str, dict]:
    """Backstop for dark peers that have no proven-live watcher.

    Each peer may declare which other watched peers cover its lone outage. A
    dark peer is suppressed only while one of those declared watchers has a
    fresh heartbeat. Missing declarations preserve the older mutual-watch rule
    by treating every other watched peer as a potential watcher.

    The observer itself remains covered by the TMN host's ordinary peer list;
    this function chooses alert ownership only for the peers the observer reads.
    """
    peers = cfg.get("peers") or []
    verdicts, statuses, dark, details = {}, {}, [], []
    for peer in peers:
        label = peer.get("label") or peer.get("url", "peer")
        status, detail, n = read_peer({"peer": peer}, int(fails.get(label, 0)))
        verdicts[label] = n
        statuses[label] = status
        details.append(f"{label}: {detail or status}")
        if status == "peer":
            dark.append(label)

    if peers and len(dark) == len(peers):
        return "peer", ("every watched box is dark — " + "; ".join(details) +
                        ". Nothing is monitoring either Hermes bot."), verdicts
    live = {label for label, status in statuses.items() if status == "ok"}
    labels = set(statuses)
    uncovered = []
    for peer in peers:
        label = peer.get("label") or peer.get("url", "peer")
        if label not in dark:
            continue
        covered_by = peer.get("covered_by")
        watchers = set(covered_by) if covered_by is not None else labels - {label}
        if live.isdisjoint(watchers):
            uncovered.append(label)
    if uncovered:
        return "peer", (
            "watched box has no live coverage — "
            + "; ".join(detail for detail in details if detail.split(":", 1)[0] in uncovered)
        ), verdicts
    return "ok", "; ".join(details), verdicts


GATEWAY_TRANSIENT_STATES = frozenset({"activating", "deactivating", "reloading"})
GATEWAY_SETTLE_ATTEMPTS = 12
GATEWAY_SETTLE_INTERVAL_S = 1


def gateway_active(unit: str) -> tuple[bool, str]:
    """Is the gateway running after any in-flight systemd transition settles?

    A gateway restart normally spends a few seconds in ``deactivating`` and
    ``activating``. Paging from a single sample in that window produces a false
    Codex re-auth alert even though the credential is healthy and systemd is
    already restoring the bot. Stable inactive/failed states still fail fast.
    """
    try:
        state = "unknown"
        for attempt in range(GATEWAY_SETTLE_ATTEMPTS + 1):
            r = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True, timeout=20,
            )
            state = r.stdout.strip() or "unknown"
            if state == "active":
                return True, state
            if state not in GATEWAY_TRANSIENT_STATES:
                return False, state
            if attempt < GATEWAY_SETTLE_ATTEMPTS:
                time.sleep(GATEWAY_SETTLE_INTERVAL_S)
        return False, state
    except Exception as e:
        return True, f"uncheckable ({type(e).__name__})"


def detect(auth_path: pathlib.Path, gateway_unit: str) -> tuple[str, str]:
    try:
        d = json.loads(auth_path.read_text())
    except Exception as e:
        raise Disarmed(f"cannot read {auth_path}: {type(e).__name__}: {e}")

    prov = (d.get("providers") or {}).get(PROVIDER) or {}
    pool = (d.get("credential_pool") or {}).get(PROVIDER) or []
    if not prov and not pool:
        return "down", "no Codex credential at all (providers block empty, pool empty)"

    pooled = bool(pool)
    if pooled:
        renewable = renewable_pool_entries(d)
        if not renewable:
            return "down", (f"credential pool has {len(pool)} entr"
                            f"{'y' if len(pool) == 1 else 'ies'} but no renewable "
                            "pooled credential")
        now = time.time()
        active = next((entry for entry in renewable
                       if not entry_quota_blocked(entry, now, QUOTA_STALE_S)[0]),
                      renewable[0])
        prov = active

    toks = prov.get("tokens") or prov
    refresh = toks.get("refresh_token") or ""
    access = toks.get("access_token") or ""
    exp = _exp_of(access)
    now = time.time()
    fp = lineage(refresh)

    if not pooled:
        err = prov.get("last_auth_error") or {}
        if err:
            code = str(err.get("code") or "")
            relogin = bool(err.get("relogin_required"))
            at = str(err.get("at") or "")
            last_refresh = str(prov.get("last_refresh") or "")
            # Only trust an error newer than the last successful refresh; a stale
            # record from a since-repaired outage must not page forever.
            if at and last_refresh and at > last_refresh:
                if relogin or code == "refresh_token_reused":
                    return "down", (f"{code or 'auth error'} at {at} "
                                    f"(relogin_required={relogin}, lineage={fp})")

        if not refresh:
            return "down", "credential has NO refresh token — it cannot renew and will not recover on its own"

        if exp and exp <= now:
            return "down", (f"access token expired "
                            f"{time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(exp))} "
                            f"and nothing refreshed it (lineage={fp})")

    up, raw = gateway_active(gateway_unit)
    if not up:
        return "down", (f"codex credential is healthy but {gateway_unit} is {raw} — "
                        f"the bot is down for a different reason (lineage={fp})")

    plan = plan_of(access)
    plan_note = f", plan={plan}" if plan else ""

    # Last, deliberately: sign-in and a dead gateway are the more actionable
    # failures, and quota only matters once the credential is otherwise fine.
    out_of_quota, quota_detail = quota_blocked(d)
    if out_of_quota:
        return "quota", (f"{quota_detail}. The credential itself is valid "
                         f"(lineage={fp}{plan_note}) — signing in again will not help.")

    when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(exp)) if exp else "unknown"
    if pooled:
        label = str(prov.get("label") or prov.get("id") or "unnamed")
        return "ok", (f"pooled credential {label} renewable "
                      f"(lineage={fp}{plan_note}), access token valid to {when}, "
                      f"gateway {raw}")
    return "ok", (f"refresh token present (lineage={fp}{plan_note}), "
                  f"access token valid to {when}, gateway {raw}")


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------

def send_email(cfg_ch: dict, subject: str, body: str, api_key: str) -> None:
    to = list(cfg_ch["to"])
    args = {"recipient_email": to[0], "subject": subject, "body": body, "is_html": False}
    if len(to) > 1:
        args["extra_recipients"] = to[1:]
    payload = {"user_id": cfg_ch["composio_user_id"], "arguments": args}
    # Composio resolves the sending mailbox from connected_account_id; user_id
    # alone lets it default-resolve, which has picked the wrong mailbox before.
    if cfg_ch.get("connected_account_id"):
        payload["connected_account_id"] = cfg_ch["connected_account_id"]
    req = urllib.request.Request(
        COMPOSIO_EXEC,
        data=json.dumps(payload).encode(),
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        resp = json.loads(r.read())
    # Require an explicit success. The version this replaces also accepted "has a
    # data key and no error key", which let some delivery failures read as sent.
    if resp.get("successful") is not True:
        raise RuntimeError("composio email error: " + json.dumps(resp)[:400])


def slack_post(cfg_ch: dict, text: str, token: str) -> None:
    req = urllib.request.Request(
        SLACK_URL,
        data=json.dumps({"channel": cfg_ch["channel"], "text": text,
                         "unfurl_links": False}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError("slack error: " + json.dumps(resp)[:300])


def telegram_post(cfg_ch: dict, text: str, token: str) -> None:
    """Send an alert as a Telegram DM.

    Carried for the observer host, which holds no Composio or Slack credential.
    A bot token is the narrowest secret that can reach Shawn from a third box: it
    can only speak as a bot he owns, where TMN_COMPOSIO_API_KEY could send mail as
    him. It also keeps working when both Hermes boxes are off, since Telegram is a
    cloud API and does not care whether either gateway is running.
    """
    payload = {"chat_id": cfg_ch["chat_id"], "text": text,
               "disable_web_page_preview": True}
    if cfg_ch.get("thread_id"):
        payload["message_thread_id"] = int(cfg_ch["thread_id"])
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError("telegram error: " + json.dumps(resp)[:300])


def linear_create(cfg_ch: dict, title: str, description: str, api_key: str) -> str:
    query = """
    mutation IssueCreate($teamId: String!, $title: String!, $description: String!, $assigneeId: String!) {
      issueCreate(input: {teamId: $teamId, title: $title, description: $description, assigneeId: $assigneeId}) {
        success
        issue { id identifier url }
      }
    }"""
    req = urllib.request.Request(
        LINEAR_URL,
        data=json.dumps({"query": query, "variables": {
            "teamId": cfg_ch["team"], "title": title,
            "description": description, "assigneeId": cfg_ch["assignee"],
        }}).encode(),
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        resp = json.load(r)
    if resp.get("errors"):
        raise RuntimeError("linear error: " + json.dumps(resp["errors"])[:300])
    issue = (((resp.get("data") or {}).get("issueCreate") or {}).get("issue")) or {}
    return issue.get("url") or issue.get("identifier") or "(created)"




# --------------------------------------------------------------------------
# message bodies
# --------------------------------------------------------------------------

def _joined(cfg: dict, key: str) -> str:
    return "\n".join(cfg.get(key) or [])


def subject(cfg: dict, status: str) -> str:
    """Email subject. A quota outage must not arrive titled "lost its sign-in".

    Falls back to the sign-in subject when a host config predates
    ``quota_subject``, so an un-migrated host still pages.
    """
    if status == "quota":
        return cfg.get("quota_subject") or (
            f"Action needed: {cfg['bot_label']} is out of Codex quota "
            f"({cfg['host_label']})")
    if status == "peer":
        return "Action needed: a peer watchdog has gone quiet"
    if status == "sibling":
        return f"Action needed: a watchdog on {cfg['host_label']} has stopped running"
    return cfg["subject"]


def ticket_title(cfg: dict, status: str) -> str:
    if status == "quota":
        return cfg.get("quota_ticket_title") or (
            f"Codex quota exhausted — {cfg['bot_label']} ({cfg['host_label']})")
    if status == "peer":
        return f"Peer watchdog silent — reported by {cfg['host_label']}"
    if status == "sibling":
        return f"Watchdog timer stopped on {cfg['host_label']}"
    return cfg["ticket_title"]


def reauth_line(cfg: dict) -> str:
    """The sign-in URL, surfaced up top where it is actually readable.

    Deliberately paired with the caveat. The device page cannot be completed on
    its own — it wants a code that only step 1 produces — so an unqualified link
    at the top of an alert invites opening a page you cannot finish, at exactly
    the moment you are least inclined to read further.
    """
    url = cfg.get("reauth_url")
    if not url:
        return ""
    return (f"Reauth here: {url}\n"
            f"  (start at step 1 below first — this page needs the device code "
            f"that the CLI prints)\n\n")


PEER_REMEDY = [
    "This alert is about the OTHER box. Nothing is wrong with the credential on",
    "the host that sent it.",
    "",
    "The peer's watchdog has stopped running, so that bot is now unmonitored —",
    "whatever breaks there next will go unreported. Check, on the peer:",
    "",
    "  systemctl --user status <its health timer>",
    "  systemctl --user list-timers | grep health",
    "  journalctl --user -u <its health service> -n 50",
    "",
    "Most likely: the timer was disabled or never re-enabled after maintenance",
    "(2026-08-04), the box is off, or lingering was lost so its user units never",
    "started at boot.",
]


def peer_alert_text(cfg: dict, detail: str, ticket: str | None) -> str:
    """Alert body for a dark peer. Deliberately not the sign-in prose.

    Sending the reauth runbook here would point an operator at the wrong machine
    entirely, which is worse than saying nothing.
    """
    peer = cfg.get("peer") or {}
    return (
        f"The watchdog on {peer.get('label', 'the peer box')} has gone quiet. "
        f"Reported by {cfg['host_label']}, whose own check is fine.\n\n"
        f"What the check saw: {detail}\n\n"
        + (f"Linear ticket: {ticket}\n\n" if ticket else "")
        + "\n".join(PEER_REMEDY) + "\n\n"
        "You will not get another message for this outage. A new alert can fire "
        "only after the peer reports again and later goes quiet.\n\n"
        f"-- Automated check on {cfg['host_label']} (codex-health, peer watch)."
    )


QUOTA_REMEDY = [
    "This is NOT a sign-in problem. Do not run the device-code login — it will",
    "complete successfully and change nothing.",
    "",
    "The options are:",
    "  1. Wait for the window to roll (the reset time is above).",
    "  2. Raise the plan on the ChatGPT account the credential is attached to.",
    "  3. Point the profile at another provider for now (`model.provider` in the",
    "     profile config.yaml), then restart the gateway unit.",
    "",
    "Confirm which account is attached before buying anything — an upgrade on the",
    "wrong account looks identical from here and fixes nothing.",
]


def quota_alert_text(cfg: dict, detail: str, ticket: str | None) -> str:
    """Alert body for a quota outage.

    Shares nothing with the auth body on purpose. The reauth URL is omitted
    entirely: it is the wrong action, and offering it is how 2026-08-17 spent two
    completed logins on a plan that was simply out of quota.
    """
    return (
        f"{cfg['bot_label']} on {cfg['host_label']} is out of Codex quota, so it has "
        f"stopped answering. Its sign-in is fine.\n\n"
        f"What the check saw: {detail}\n\n"
        + (f"Linear ticket: {ticket}\n\n" if ticket else "")
        + "\n".join(QUOTA_REMEDY) + "\n\n"
        + (_joined(cfg, "quota_note") + "\n\n" if cfg.get("quota_note") else "")
        + "You will not get another message about this for 24 hours, and none at all "
        "once it is working again.\n\n"
        f"-- Automated check on {cfg['host_label']} (codex-health)."
    )


SIBLING_REMEDY = [
    "Nothing is wrong with this box's own credential or gateway. A DIFFERENT",
    "watchdog running here has stopped, so whatever it guards is now unmonitored.",
    "",
    "  systemctl --user status <the timer named above>",
    "  systemctl --user enable --now <that timer>",
    "",
    "A timer switched off during maintenance and never re-enabled is exactly how",
    "the 2026-08-04 outage ran six days before anyone noticed.",
]


def sibling_alert_text(cfg: dict, detail: str, ticket: str | None) -> str:
    return (
        f"A watchdog on {cfg['host_label']} has stopped running. Reported by the "
        f"codex check on the same box, which is itself fine.\n\n"
        f"What the check saw: {detail}\n\n"
        + (f"Linear ticket: {ticket}\n\n" if ticket else "")
        + "\n".join(SIBLING_REMEDY) + "\n\n"
        "You will not get another message about this for 24 hours, and none at all "
        "once it is armed again.\n\n"
        f"-- Automated check on {cfg['host_label']} (codex-health, sibling watch)."
    )


def alert_text(cfg: dict, detail: str, ticket: str | None, status: str = "down") -> str:
    if status == "sibling":
        return sibling_alert_text(cfg, detail, ticket)
    if status == "quota":
        return quota_alert_text(cfg, detail, ticket)
    if status == "peer":
        return peer_alert_text(cfg, detail, ticket)
    return (
        f"{cfg['bot_label']} on {cfg['host_label']} can no longer sign in to Codex, "
        f"so it has stopped answering.\n\n"
        f"What the check saw: {detail}\n\n"
        + reauth_line(cfg)
        + (f"Linear ticket: {ticket}\n\n" if ticket else "")
        + _joined(cfg, "runbook") + "\n\n"
        + _joined(cfg, "context_note") + "\n\n"
        "You will not get another message about this for 24 hours, and none at all "
        "once it is working again.\n\n"
        f"-- Automated check on {cfg['host_label']} (codex-health)."
    )


def ticket_body(cfg: dict, detail: str, status: str = "down") -> str:
    if status == "sibling":
        return (
            f"A watchdog on `{cfg['host_label']}` has stopped running; its target is "
            f"unmonitored. Filed by the codex check on the same box.\n\n"
            f"**What the check saw:** {detail}\n\n"
            f"### Fix\n\n```\n" + "\n".join(SIBLING_REMEDY) + "\n```\n\n"
            f"_Auto-filed by the {cfg['host_label']} codex-health check._"
        )
    if status == "peer":
        peer = cfg.get("peer") or {}
        return (
            f"The watchdog on `{peer.get('label', 'the peer box')}` has stopped "
            f"reporting. Filed by `{cfg['host_label']}`, whose own check is healthy.\n\n"
            f"**What the check saw:** {detail}\n\n"
            f"### Fix\n\n```\n" + "\n".join(PEER_REMEDY) + "\n```\n\n"
            f"_Auto-filed by the {cfg['host_label']} codex-health check._"
        )
    if status == "quota":
        return (
            f"`{cfg['bot_label']}` on `{cfg['host_label']}` is out of Codex quota, so it "
            f"has stopped answering. **The sign-in is fine — a re-login will not help.**\n\n"
            f"**What the check saw:** {detail}\n\n"
            f"### Fix\n\n```\n" + "\n".join(QUOTA_REMEDY) + "\n```\n\n"
            + (f"### Context\n\n{_joined(cfg, 'quota_note')}\n\n"
               if cfg.get("quota_note") else "")
            + f"_Auto-filed by the {cfg['host_label']} codex-health check._"
        )
    url = cfg.get("reauth_url")
    link = (f"**Reauth here:** {url}\n"
            f"(start at step 1 — this page needs the device code the CLI prints)\n\n"
            if url else "")
    return (
        f"The Hermes gateway on `{cfg['host_label']}` can no longer sign in to Codex, "
        f"so `{cfg['bot_label']}` has stopped answering.\n\n"
        f"**What the check saw:** {detail}\n\n"
        + link
        + f"### Fix\n\n```\n{_joined(cfg, 'runbook')}\n```\n\n"
        f"### Context\n\n{_joined(cfg, 'context_note')}\n\n"
        f"_Auto-filed by the {cfg['host_label']} codex-health check._"
    )


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"status": "ok", "last_alert": 0, "ticket_url": None}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        # Do NOT fall back to a default here. Silently resetting to "ok" would
        # turn an ongoing outage into a fabricated recovery and re-arm the edge
        # detector, so the next check reports a brand-new first failure.
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
    # An observer has no auth store. Its secrets live beside the script, so point
    # env_val's .env search there rather than at a Hermes home it does not have.
    if cfg.get("mode") == "observer":
        hermes_home = HERE
        auth_path = config_yaml = None
    else:
        hermes_home = pathlib.Path(os.path.expanduser(cfg["hermes_home"]))
        auth_path = hermes_home / "auth.json"
        config_yaml = hermes_home / "config.yaml"
    state_path = pathlib.Path(args.state_file).expanduser() if args.state_file else HERE / "state.json"
    renotify_s = int(cfg.get("renotify_s", 24 * 3600))

    # Always say which credential was inspected. Reading the wrong auth store is
    # the highest-consequence bug available here and it is invisible otherwise.
    print(f"host={cfg['host_label']} auth={auth_path or '(observer: none)'} state={state_path}")

    observer = cfg.get("mode") == "observer"

    if not observer and not args.force_down and not gateway_uses_codex(config_yaml):
        print(f"{cfg['host_label']} gateway is not configured on {PROVIDER} — not applicable, silent")
        return 0

    st = load_state(state_path)
    prev = st.get("status", "ok")
    now = int(time.time())

    if observer:
        if args.force_peer:
            status, detail = "peer", "forced by --force-peer"
        else:
            status, detail, obs_fails = observe_peers(cfg, st.get("peer_fails") or {})
            st["peer_fails"] = obs_fails
            print(f"observer: {detail}")
        peer_fails = st.get("peer_fails") or {}
        return _decide_and_deliver(args, cfg, hermes_home, st, state_path,
                                   prev, status, detail, now, renotify_s)

    status, detail = (("down", "forced by --force-down") if args.force_down
                      else detect(auth_path, cfg["gateway_unit"]))

    # The peer watch runs only when this box is healthy. With a local failure in
    # hand, the peer is the less urgent of two problems and would bury it.
    # A sibling timer that has stopped is local, certain, and nothing else can
    # see it — so it outranks the peer watch, which is remote and inferential.
    if status == "ok" and not args.force_peer:
        sstatus, sdetail = sibling_timers_ok(cfg)
        if sstatus == "sibling":
            status, detail = "sibling", sdetail
        elif sdetail:
            print(f"siblings: {sdetail}")

    raw_fails = st.get("peer_fails")
    peer_fails = raw_fails if isinstance(raw_fails, dict) else {}
    if status == "ok" and not args.force_peer:
        pstatus, pdetail, peer_fails = read_peers(cfg, peer_fails)
        if pstatus == "peer":
            status, detail = "peer", pdetail
        elif pdetail:
            print(f"peer: {pdetail}")
    elif args.force_peer:
        status, detail = "peer", "forced by --force-peer"
    st["peer_fails"] = peer_fails

    return _decide_and_deliver(args, cfg, hermes_home, st, state_path,
                               prev, status, detail, now, renotify_s)


def _decide_and_deliver(args, cfg, hermes_home, st, state_path, prev, status,
                        detail, now, renotify_s) -> int:
    """The edge-triggered state machine and delivery, shared by both paths.

    Extracted when the observer host arrived: it reaches the same alerting rules
    by a different route (it has no credential to inspect), and duplicating this
    logic would let the two drift until one of them stopped paging.
    """
    if status == "unknown":
        print(f"status=unknown ({detail}) — no action, state untouched")
        return 0

    alert = False
    if status != "ok":
        # A change of failure KIND re-alerts even inside the quiet window. An
        # auth outage and a quota outage need opposite actions, so inheriting the
        # other one's silence would leave the wrong instruction standing.
        if prev == "ok":
            alert, why = True, f"ok->{status} (first failure)"
        elif prev != status:
            alert, why = True, f"{prev}->{status} (failure kind changed)"
        elif status == "peer":
            why = "still peer, quiet until recovery"
        elif now - int(st.get("last_alert", 0)) >= renotify_s:
            alert, why = True, f"still {status}, reminder"
        else:
            why = f"still {status}, inside quiet window"
    else:
        why = "healthy (silent by design)" if prev == "ok" else "recovered (silent re-arm)"

    channels = cfg["channels"]

    if args.dry_run:
        print(f"status={status} prev={prev} detail={detail!r} action={why}")
        if alert:
            for name in ("slack", "email", "linear", "telegram"):
                ch = channels.get(name)
                if not ch or getattr(args, f"no_{name}"):
                    continue
                if name == "linear":
                    print(f"\n--- LINEAR -> team {ch['team']} ---\n{ticket_title(cfg, status)}")
                elif name in ("slack", "telegram"):
                    print(f"\n--- SLACK -> {ch['channel']} ---\n"
                          f"{alert_text(cfg, detail, None, status)}")
                else:
                    print(f"\n--- EMAIL -> {ch['to']} ---\n"
                          f"Subject: {subject(cfg, status)}\n\n"
                          f"{alert_text(cfg, detail, None, status)}")
        return 0

    delivered, failures = 0, []

    if alert:
        ticket = st.get("ticket_url")
        # One ticket per outage: file on the first failure, not on reminders. A
        # changed failure kind is a new problem with a different fix, so it earns
        # its own ticket rather than a comment on one that says to re-login.
        ch = channels.get("linear")
        if ch and not args.no_linear and prev != status:
            key = env_val(ch["key_env"], hermes_home)
            if not key:
                failures.append(f"linear: {ch['key_env']} unresolvable")
            else:
                try:
                    ticket = linear_create(ch, ticket_title(cfg, status),
                                           ticket_body(cfg, detail, status), key)
                    st["ticket_url"] = ticket
                    delivered += 1
                    print(f"linear ticket: {ticket}")
                except Exception as e:
                    failures.append(f"linear: {type(e).__name__}: {e}")

        ch = channels.get("slack")
        if ch and not args.no_slack:
            tok = env_val(ch["token_env"], hermes_home)
            if not tok:
                failures.append(f"slack: {ch['token_env']} unresolvable")
            else:
                try:
                    slack_post(ch, alert_text(cfg, detail, ticket, status), tok)
                    delivered += 1
                    print(f"slack -> {ch['channel']}")
                except Exception as e:
                    failures.append(f"slack: {type(e).__name__}: {e}")

        ch = channels.get("telegram")
        if ch and not args.no_telegram:
            tok = env_val(ch["token_env"], hermes_home)
            if not tok:
                failures.append(f"telegram: {ch['token_env']} unresolvable")
            else:
                try:
                    telegram_post(ch, alert_text(cfg, detail, ticket, status), tok)
                    delivered += 1
                    print(f"telegram -> {ch['chat_id']}")
                except Exception as e:
                    failures.append(f"telegram: {type(e).__name__}: {e}")

        ch = channels.get("email")
        if ch and not args.no_email:
            key = env_val(ch["key_env"], hermes_home)
            if not key:
                failures.append(f"email: {ch['key_env']} unresolvable")
            else:
                try:
                    send_email(ch, subject(cfg, status),
                               alert_text(cfg, detail, ticket, status), key)
                    delivered += 1
                    print(f"emailed {ch['to']}")
                except Exception as e:
                    failures.append(f"email: {type(e).__name__}: {e}")

        st["last_alert"] = now
        print(f"status={status} ({why}) | {detail}")
    else:
        if status == "ok":
            st["ticket_url"] = None      # re-arm so the next outage files a fresh ticket
        print(f"status={status} action={why} | {detail}")

    st["status"] = status
    save_state(state_path, st)

    # Written last and unconditionally: the peer is asking "did this check run",
    # not "did it like what it found".
    write_heartbeat(state_path.parent / "heartbeat.json", cfg, status)

    # An outage nobody was told about is indistinguishable from no outage. If we
    # decided to alert and every channel failed, that is a hard failure -- the
    # version this replaces printed the errors and still exited 0. It surfaces in
    # `systemctl --user status` and the journal; by explicit decision there is no
    # second escalation channel, so this is the only trace.
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
        description="Alert when a Hermes host's Codex sign-in breaks. Detect only; never repairs.")
    ap.add_argument("--config", default=str(HERE / "config.json"),
                    help="per-host config (default: config.json next to this script)")
    ap.add_argument("--state-file", default=None,
                    help="override state path — use for drills so they cannot "
                         "write production state and suppress a real outage")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-down", action="store_true",
                    help="exercise the alert path (bypasses detection entirely)")
    ap.add_argument("--force-peer", action="store_true",
                    help="exercise the peer-down alert path (bypasses the peer fetch)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-slack", action="store_true")
    ap.add_argument("--no-linear", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()
    try:
        return run(args)
    except Disarmed as e:
        print(f"WATCHDOG DISARMED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
