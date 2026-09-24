"""Tests for the canonical codex watchdog.

The repo previously had zero coverage of the watchdog, which is uncomfortable for
code whose entire job is to be trustworthy when everything else is broken.

Two things matter most here and get the most attention:

  1. Which auth store each host resolves to. Unifying two scripts that disagreed
     on this default is the highest-consequence bug available: pointing the TMN
     box at its stale root auth.json yields a confident, wrong "ok".
  2. That every way the watchdog can stop working is LOUD. A monitor that fails
     silently is worse than no monitor, because it also removes the suspicion
     that you are unmonitored.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import sys
import time

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
WATCHDOG = REPO / "watchdog"


def _load(name: str):
    sys.path.insert(0, str(WATCHDOG))
    spec = importlib.util.spec_from_file_location(name, WATCHDOG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load("codex_health_check")
ORIGINAL_GATEWAY_ACTIVE = chk.gateway_active


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def jwt_with_exp(exp: int | None, plan: str | None = None) -> str:
    """A syntactically valid JWT carrying just the claims detect() reads."""
    auth = {"chatgpt_account_id": "acct-test"}
    if plan is not None:
        auth["chatgpt_plan_type"] = plan
    claims = {"https://api.openai.com/auth": auth}
    if exp is not None:
        claims["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def pool_entry(*, label="cred-1", exhausted=True, reset_offset=+3600,
               status_age_s=60, code=429, in_message=True, refresh="rt-pool",
               exp_offset=+86400) -> dict:
    """A credential_pool entry shaped like the ones Hermes actually writes.

    Mirrors the real record from the 2026-08-17 incident: last_status
    "exhausted", last_error_code 429, and the 429 body stored as a Python repr
    (not JSON) with resets_at inside it.
    """
    exp = int(time.time()) + exp_offset if exp_offset is not None else None
    e = {"id": label, "label": label, "auth_type": "oauth",
         "access_token": jwt_with_exp(exp), "refresh_token": refresh,
         "last_status_at": time.time() - status_age_s}
    if not exhausted:
        e["last_status"] = "ok"
        return e
    e["last_status"] = "exhausted"
    e["last_error_code"] = code
    reset = int(time.time()) + reset_offset if reset_offset is not None else None
    body = ("Error code: 429 - {'error': {'type': 'usage_limit_reached', "
            "'message': 'The usage limit has been reached', 'plan_type': 'plus'"
            + (f", 'resets_at': {reset}" if (reset and in_message) else "")
            + "}}")
    e["last_error_message"] = body
    return e


def auth_doc(*, refresh="rt-aaa", exp_offset=+86400, last_error=None,
             last_refresh="2026-08-01T00:00:00Z", empty=False,
             pool=None, plan=None) -> dict:
    if empty:
        return {"providers": {}, "credential_pool": {}}
    exp = int(time.time()) + exp_offset if exp_offset is not None else None
    prov = {"tokens": {"access_token": jwt_with_exp(exp, plan)}, "last_refresh": last_refresh}
    if refresh:
        prov["tokens"]["refresh_token"] = refresh
    if last_error:
        prov["last_auth_error"] = last_error
    doc = {"providers": {"openai-codex": prov}}
    if pool is not None:
        doc["credential_pool"] = {"openai-codex": pool}
    return doc


def write_host(tmp_path, name: str, hermes_home, **overrides) -> pathlib.Path:
    cfg = {
        "host_label": name,
        "bot_label": f"@{name}",
        "gateway_unit": f"hermes-gateway-{name}.service",
        "subject": "s", "ticket_title": "t",
        "channels": {"email": {"to": ["x@y.z"], "key_env": "K", "composio_user_id": "u"}},
        "runbook": ["do the thing"], "context_note": ["because"],
    }
    if hermes_home is not None:
        cfg["hermes_home"] = hermes_home
    cfg.update(overrides)
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(cfg))
    return p


@pytest.fixture(autouse=True)
def gateway_up(monkeypatch):
    """Default the gateway to running so auth assertions stay isolated."""
    monkeypatch.setattr(chk, "gateway_active", lambda unit: (True, "active"))


# --------------------------------------------------------------------------
# config — the no-default rule
# --------------------------------------------------------------------------

def test_missing_hermes_home_is_fatal_not_defaulted(tmp_path):
    """The whole point. A missing auth store must stop the run, never be guessed.

    The two scripts this replaces disagreed (~/.hermes vs ~/.hermes/profiles/tmn).
    Any shared default silently points one host at the wrong credential.
    """
    cfg = write_host(tmp_path, "nohome", hermes_home=None)
    with pytest.raises(chk.Disarmed, match="hermes_home"):
        chk.load_config(cfg)


def test_unreadable_config_is_fatal(tmp_path):
    with pytest.raises(chk.Disarmed, match="cannot read config"):
        chk.load_config(tmp_path / "does-not-exist.json")


def test_config_with_no_channels_is_fatal(tmp_path):
    cfg = write_host(tmp_path, "nochan", "~/.hermes", channels={})
    with pytest.raises(chk.Disarmed, match="no alert channels"):
        chk.load_config(cfg)


def test_shipped_host_configs_resolve_to_the_right_auth_store():
    """Each post-migration config names the auth store its gateway uses."""
    tmn = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    assert tmn["hermes_home"] == "~/.hermes"
    assert tmn["gateway_unit"] == "hermes-gateway.service"

    host = chk.load_config(WATCHDOG / "hosts" / "src.json")
    assert host["hermes_home"] == "~/.hermes"
    assert host["gateway_unit"] == "hermes-gateway.service"


def test_alert_surfaces_the_reauth_link_with_its_caveat():
    """The link must be near the top, and must say it needs the device code.

    It was previously buried in step 3 of the runbook prose — unreadable at the
    moment you actually need it. But an unqualified link is worse than none: the
    device page cannot be completed without a code the CLI prints first.
    """
    for host in ("src", "tmn"):
        cfg = chk.load_config(WATCHDOG / "hosts" / f"{host}.json")
        body = chk.alert_text(cfg, "detail", None)
        assert "https://auth.openai.com/codex/device" in body
        assert "device code" in body

        # near the top: before the runbook, not buried inside it
        assert body.index("Reauth here:") < body.index("1. SSH")

        ticket = chk.ticket_body(cfg, "detail")
        assert "https://auth.openai.com/codex/device" in ticket


def test_alert_omits_the_reauth_line_when_no_url_configured(tmp_path):
    cfg = chk.load_config(write_host(tmp_path, "nolink", "~/.hermes"))
    assert "Reauth here" not in chk.alert_text(cfg, "detail", None)


def test_company_alerts_reach_the_team_and_personal_ones_do_not():
    """Set 2026-08-17: outages page Shawn, Ian and Abraham — except @Screddy_bot.

    Shawn's personal assistant going down is not an Ian or Abraham problem, and
    routing it to them trains three people to ignore the channel.
    """
    team = {"shawn@teamnebula.ai", "ian@teamnebula.ai", "abraham@teamnebula.ai"}

    for host in ("tmn", "nebos-claude"):
        # read as JSON: nebos-claude.json is the Claude watchdog's config and has
        # no hermes_home, so the codex loader rightly refuses it
        cfg = json.loads((WATCHDOG / "hosts" / f"{host}.json").read_text())
        assert set(cfg["channels"]["email"]["to"]) == team, host

    personal = chk.load_config(WATCHDOG / "hosts" / "src.json")
    assert personal["channels"]["email"]["to"] == ["shawn@teamnebula.ai"]
    assert "ian@teamnebula.ai" not in personal["channels"]["email"]["to"]

    # The dedicated TMN observer uses its own Telegram route and does not borrow
    # the Team Nebula Slack credential.
    obs = chk.load_config(WATCHDOG / "hosts" / "tmn-observer.json")
    assert sorted(obs["channels"]) == ["telegram"]
    assert obs["channels"]["telegram"]["chat_id"] == "6056863584"


def test_shipped_codex_hosts_route_alerts_per_host():
    """Routing set by instruction on 2026-08-17, amended 2026-08-24.

    src (@Screddy_bot) is Shawn's own assistant, so it stays out of the
    team channel: it DMs him over Telegram and emails him. Telegram was added on
    2026-08-24 so the primary path shares no failure mode with Composio, after a
    key rotation left email 401ing with only the OnFailure backstop delivering.
    neb-ops-gcp (@Teamnebula_bot) is company infrastructure, so it posts to the
    team channel and emails. Linear ticketing was dropped from src on 2026-08-17.
    """
    host = chk.load_config(WATCHDOG / "hosts" / "src.json")
    assert sorted(host["channels"]) == ["email", "telegram"]
    assert host["channels"]["email"]["to"] == ["shawn@teamnebula.ai"]
    assert host["channels"]["telegram"]["token_env"] == "TELEGRAM_BOT_TOKEN"

    tmn = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    assert sorted(tmn["channels"]) == ["email", "slack"]
    assert tmn["channels"]["slack"]["channel"] == "C09FLJDCAJD"        # #tmn-ops
    assert tmn["channels"]["slack"]["token_env"] == "SLACK_BOT_TOKEN"
    assert "shawn@teamnebula.ai" in tmn["channels"]["email"]["to"]

    # The personal box must not page the team channel.
    assert "slack" not in host["channels"]


def test_shipped_email_channels_pin_entity_and_account():
    """The 2026-08-21 Composio migration retired the shared entity user_uwgmr.

    Every shipped email channel must pin a per-mailbox user_id AND an explicit
    connected_account_id: the retired entity fails even with a live key, which
    is how the 2026-08-24 outage looked like a credential problem from the
    outside. Default-resolution is also how mail has been sent from the wrong
    mailbox before, so the account id is not optional politeness.
    """
    for name in ("src", "tmn"):
        ch = chk.load_config(WATCHDOG / "hosts" / f"{name}.json")["channels"]["email"]
        assert ch["composio_user_id"] != "user_uwgmr"
        assert ch.get("connected_account_id", "").startswith("ca_")


def test_shipped_configs_carry_their_own_runbook_only():
    """Each host's runbook must not tell an operator to log into the other box."""
    tmn = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    host = chk.load_config(WATCHDOG / "hosts" / "src.json")
    tmn_rb, host_rb = "\n".join(tmn["runbook"]), "\n".join(host["runbook"])

    assert "ssh neb-ops-gcp" in tmn_rb and "hermes-gateway.service" in tmn_rb
    assert "--profile tmn" not in tmn_rb
    assert "ssh screddy-hermes" not in tmn_rb

    assert "ssh screddy-hermes" in host_rb
    assert "--profile tmn" not in host_rb and "tunnel-through-iap" not in host_rb


def test_tmn_observer_runbook_uses_the_verified_vm_alias():
    observer = chk.load_config(WATCHDOG / "hosts" / "tmn-observer.json")
    runbook = "\n".join(observer["runbook"])

    assert observer["host_label"] == "tmn-codex-observer"
    assert "ssh r2h-hermes" in runbook
    assert "ssh screddy-hermes" not in runbook


def test_src_runbook_describes_bounded_refresh_and_bans_ad_hoc_token_edits():
    host = chk.load_config(WATCHDOG / "hosts" / "src.json")
    note = "\n".join(host["context_note"])

    assert "pinned direct-refresh helper" in note
    assert "Do not edit token fields by hand" in note
    assert "no longer any scheduled token refresh" not in note
    assert "Nothing will retry it for you" not in note


# --------------------------------------------------------------------------
# detect()
# --------------------------------------------------------------------------

def test_healthy_credential(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc()))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "ok"
    assert "lineage=" in detail


def test_no_refresh_token_is_down(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "down" and "NO refresh token" in detail


def test_expired_access_token_is_down(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(exp_offset=-3600)))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "down" and "expired" in detail


def test_no_credential_at_all_is_down(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(empty=True)))
    status, _ = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "down"


def test_relogin_required_newer_than_last_refresh_is_down(tmp_path):
    err = {"code": "relogin_required", "relogin_required": True,
           "at": "2026-08-09T00:00:00Z"}
    (tmp_path / "auth.json").write_text(json.dumps(
        auth_doc(last_error=err, last_refresh="2026-08-01T00:00:00Z")))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "down" and "relogin_required=True" in detail


def test_stale_auth_error_older_than_last_refresh_is_ignored(tmp_path):
    """A repaired outage leaves its error record behind; it must not page forever."""
    err = {"code": "refresh_token_reused", "relogin_required": True,
           "at": "2026-07-01T00:00:00Z"}
    (tmp_path / "auth.json").write_text(json.dumps(
        auth_doc(last_error=err, last_refresh="2026-08-01T00:00:00Z")))
    status, _ = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "ok"


def test_healthy_pool_outranks_broken_legacy_singleton(tmp_path):
    """Hermes routes through the pool, so a stale singleton must not page.

    This mirrors Screddy's live auth store: the singleton recorded
    refresh_token_reused while a manual pooled credential served real Codex
    requests. The watchdog reported the working gateway as signed out.
    """
    err = {"code": "refresh_token_reused", "relogin_required": True,
           "at": "2026-08-27T10:19:44Z"}
    doc = auth_doc(refresh=None, last_error=err,
                   last_refresh="2026-08-21T09:06:21Z",
                   pool=[pool_entry(label="chatgpt-teamnebula", exhausted=False)])
    (tmp_path / "auth.json").write_text(json.dumps(doc))

    status, detail = chk.detect(tmp_path / "auth.json", "u.service")

    assert status == "ok"
    assert "chatgpt-teamnebula" in detail
    assert "pooled credential" in detail


def test_unusable_pool_is_down_even_when_legacy_singleton_is_healthy(tmp_path):
    """A populated pool is Hermes' runtime source of truth, not the singleton."""
    doc = auth_doc(pool=[pool_entry(label="broken-pool", exhausted=False,
                                    refresh=None)])
    (tmp_path / "auth.json").write_text(json.dumps(doc))

    status, detail = chk.detect(tmp_path / "auth.json", "u.service")

    assert status == "down"
    assert "no renewable pooled credential" in detail


def test_unreadable_auth_is_disarmed_not_silently_ok(tmp_path):
    with pytest.raises(chk.Disarmed):
        chk.detect(tmp_path / "missing.json", "u.service")


def test_malformed_auth_json_is_disarmed(tmp_path):
    (tmp_path / "auth.json").write_text("{not json")
    with pytest.raises(chk.Disarmed):
        chk.detect(tmp_path / "auth.json", "u.service")


def test_dead_gateway_is_down_even_with_valid_credential(tmp_path, monkeypatch):
    """Codex auth can be perfect while the bot is dead. Nothing used to notice."""
    monkeypatch.setattr(chk, "gateway_active", lambda unit: (False, "failed"))
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc()))
    status, detail = chk.detect(tmp_path / "auth.json", "hermes-gateway.service")
    assert status == "down"
    assert "hermes-gateway.service is failed" in detail


def test_gateway_restart_transition_settles_without_false_down(monkeypatch):
    """A scheduled check can overlap a normal systemd restart by a few seconds."""
    states = iter(("deactivating", "activating", "active"))
    sleeps = []

    def run(*args, **kwargs):
        return type("Result", (), {"stdout": next(states) + "\n"})()

    monkeypatch.setattr(chk.subprocess, "run", run)
    monkeypatch.setattr(chk.time, "sleep", sleeps.append)

    assert ORIGINAL_GATEWAY_ACTIVE("hermes-gateway.service") == (True, "active")
    assert sleeps == [chk.GATEWAY_SETTLE_INTERVAL_S] * 2


def test_gateway_transition_that_never_settles_is_down(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return type("Result", (), {"stdout": "deactivating\n"})()

    monkeypatch.setattr(chk.subprocess, "run", run)
    monkeypatch.setattr(chk.time, "sleep", lambda _: None)

    assert ORIGINAL_GATEWAY_ACTIVE("hermes-gateway.service") == (False, "deactivating")
    assert len(calls) == chk.GATEWAY_SETTLE_ATTEMPTS + 1


def test_lineage_is_stable_and_distinguishes_credentials():
    assert chk.lineage("rt-aaa") == chk.lineage("rt-aaa")
    assert chk.lineage("rt-aaa") != chk.lineage("rt-bbb")
    assert chk.lineage("") == "none"


# --------------------------------------------------------------------------
# quota — the blind spot that let 2026-08-15..17 read as "healthy"
# --------------------------------------------------------------------------

def test_exhausted_pool_is_quota_not_ok(tmp_path):
    """The regression this whole change exists for.

    For three days the credential refreshed perfectly, the gateway was active,
    and every user message got the canned rate-limit reply — and this check
    printed "status=ok healthy" through all of it.
    """
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(pool=[pool_entry()])))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "quota"
    assert "out of quota" in detail
    assert "signing in again will not help" in detail


def test_quota_is_distinct_from_down_so_the_advice_can_differ(tmp_path):
    """quota must not be reported as `down`; the two need opposite responses."""
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(pool=[pool_entry()])))
    assert chk.detect(tmp_path / "auth.json", "u.service")[0] != "down"


def test_spent_window_stops_paging_by_itself(tmp_path):
    """resets_at in the past means the window rolled — stale evidence, not an outage."""
    (tmp_path / "auth.json").write_text(json.dumps(
        auth_doc(pool=[pool_entry(reset_offset=-3600)])))
    assert chk.detect(tmp_path / "auth.json", "u.service")[0] == "ok"


def test_one_usable_pooled_credential_means_no_alert(tmp_path):
    """The pool is a failover set: one live entry and the bot still answers."""
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(
        pool=[pool_entry(label="dead"), pool_entry(label="live", exhausted=False)])))
    assert chk.detect(tmp_path / "auth.json", "u.service")[0] == "ok"


def test_exhausted_with_no_reset_time_falls_back_to_recency(tmp_path):
    """No resets_at to judge by: fresh evidence pages, ancient evidence does not.

    A live exhaustion re-stamps last_status_at on every attempt, so this cannot
    swallow a real outage — it only stops an old record paging forever.
    """
    fresh = pool_entry(reset_offset=None, in_message=False, status_age_s=60)
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(pool=[fresh])))
    assert chk.detect(tmp_path / "auth.json", "u.service")[0] == "quota"

    old = pool_entry(reset_offset=None, in_message=False,
                     status_age_s=chk.QUOTA_STALE_S + 600)
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(pool=[old])))
    assert chk.detect(tmp_path / "auth.json", "u.service")[0] == "ok"


def test_pooled_quota_outranks_broken_legacy_singleton(tmp_path):
    """The singleton cannot turn a live pool's quota problem into a reauth alert."""
    (tmp_path / "auth.json").write_text(json.dumps(
        auth_doc(refresh=None, pool=[pool_entry()])))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "quota"
    assert "out of quota" in detail


def test_plan_is_context_only_and_never_the_trigger(tmp_path):
    """A free plan is surfaced in the detail line but must not page on its own.

    The claim is baked at issuance and lags a plan change: on 2026-08-17 it read
    `free` while the upgraded account was already serving traffic.
    """
    (tmp_path / "auth.json").write_text(json.dumps(auth_doc(plan="free")))
    status, detail = chk.detect(tmp_path / "auth.json", "u.service")
    assert status == "ok"
    assert "plan=free" in detail


def test_quota_reset_parsed_from_the_python_repr_body():
    """Hermes stores the 429 body as a repr, not JSON, so it is matched not parsed."""
    reset = int(time.time()) + 1234
    e = {"last_status": "exhausted", "last_error_code": 429,
         "last_error_message": "Error code: 429 - {'error': {'resets_at': %d}}" % reset}
    import auth_state
    assert auth_state.reset_at_of(e) == float(reset)


def test_quota_alert_never_offers_the_reauth_link():
    """The core lesson of 2026-08-17: two device-code logins were completed against
    an exhausted plan. Offering the link here is offering the wrong action."""
    for host in ("src", "tmn"):
        cfg = chk.load_config(WATCHDOG / "hosts" / f"{host}.json")
        body = chk.alert_text(cfg, "detail", None, "quota")
        assert "auth.openai.com/codex/device" not in body
        assert "NOT a sign-in problem" in body
        assert "out of Codex quota" in body

        ticket = chk.ticket_body(cfg, "detail", "quota")
        assert "auth.openai.com/codex/device" not in ticket

        # and the sign-in alert is unchanged — it still leads with the link
        assert "auth.openai.com/codex/device" in chk.alert_text(cfg, "detail", None)


def test_quota_subject_and_title_do_not_claim_a_lost_signin():
    for host in ("src", "tmn"):
        cfg = chk.load_config(WATCHDOG / "hosts" / f"{host}.json")
        assert "quota" in chk.subject(cfg, "quota").lower()
        assert "sign-in" not in chk.subject(cfg, "quota").lower()
        assert "quota" in chk.ticket_title(cfg, "quota").lower()
        assert chk.subject(cfg, "down") == cfg["subject"]


def test_quota_prose_falls_back_when_a_host_config_predates_it(tmp_path):
    """An un-migrated host must still page, just with generic wording."""
    cfg = chk.load_config(write_host(tmp_path, "old", "~/.hermes"))
    assert "out of Codex quota" in chk.subject(cfg, "quota")
    assert "NOT a sign-in problem" in chk.alert_text(cfg, "d", None, "quota")


# --------------------------------------------------------------------------
# peer watch — the case OnFailure= cannot reach: a check that never runs
# --------------------------------------------------------------------------

def peer_cfg(url="http://peer.invalid/heartbeat", **over):
    cfg = {"peer": {"label": "hermes-tmn", "bot_label": "@Teamnebula_bot",
                    "url": url, "stale_after_s": 46800}}
    cfg["peer"].update(over)
    return cfg


def fake_peer(monkeypatch, *, age_s=None, raises=None):
    """Stand in for the peer's HTTP heartbeat endpoint."""
    import contextlib, io, json as _json

    def opener(url, timeout=0):
        if raises:
            raise raises
        body = _json.dumps({"host": "hermes-tmn", "status": "ok",
                            "at": int(time.time()) - age_s}).encode()
        return contextlib.closing(io.BytesIO(body))
    monkeypatch.setattr(chk.urllib.request, "urlopen", opener)


def test_fresh_peer_heartbeat_is_ok(monkeypatch):
    fake_peer(monkeypatch, age_s=600)
    status, detail, fails = chk.read_peer(peer_cfg(), 0)
    assert status == "ok" and fails == 0
    assert "10m old" in detail


def test_stale_peer_heartbeat_alerts_immediately(monkeypatch):
    """Readable but old means the peer box is up and its check stopped. No ambiguity."""
    fake_peer(monkeypatch, age_s=14 * 3600)
    status, detail, _ = chk.read_peer(peer_cfg(), 0)
    assert status == "peer"
    assert "14.0h ago" in detail
    assert "@Teamnebula_bot" in detail


def test_unreachable_peer_needs_two_misses_before_paging(monkeypatch):
    """These two boxes route over a DERP relay, so one miss is not evidence."""
    fake_peer(monkeypatch, raises=OSError("connection refused"))

    status, detail, fails = chk.read_peer(peer_cfg(), 0)
    assert status == "unknown" and fails == 1
    assert "1 consecutive" in detail

    status, detail, fails = chk.read_peer(peer_cfg(), fails)
    assert status == "peer" and fails == 2
    assert "unreachable" in detail


def test_recovered_peer_resets_the_failure_count(monkeypatch):
    fake_peer(monkeypatch, age_s=60)
    status, _, fails = chk.read_peer(peer_cfg(), 1)
    assert status == "ok" and fails == 0


def test_no_peer_configured_is_silent():
    assert chk.read_peer({}, 0) == ("ok", "", 0)


def test_local_failure_outranks_the_peer_watch(host, monkeypatch):
    """A broken credential here beats a dark box over there."""
    fake_peer(monkeypatch, age_s=99 * 3600)
    cfg = json.loads(pathlib.Path(host["cfg"]).read_text())
    cfg.update(peer_cfg())
    pathlib.Path(host["cfg"]).write_text(json.dumps(cfg))
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))

    chk.run(Args(host["cfg"], host["state"]))
    assert "can no longer sign in" in host["sent"][0]
    assert "has gone quiet" not in host["sent"][0]


def test_peer_alert_points_at_the_other_box_not_a_relogin():
    """Sending the sign-in runbook here would aim an operator at the wrong machine."""
    cfg = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    body = chk.alert_text(cfg, "src heartbeat stale", None, "peer")
    assert "auth.openai.com/codex/device" not in body
    assert "OTHER box" in body
    assert "src" in body
    assert "whose own check is fine" in body

    assert "auth.openai.com/codex/device" not in chk.ticket_body(cfg, "d", "peer")
    assert "gone quiet" in chk.subject(cfg, "peer")


def test_heartbeat_is_written_on_every_completed_run(host):
    """The peer asks whether the check RAN, not whether it liked what it found."""
    hb = pathlib.Path(host["state"]).parent / "heartbeat.json"

    chk.run(Args(host["cfg"], host["state"]))
    first = json.loads(hb.read_text())
    assert first["status"] == "ok" and first["at"] > 0

    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    chk.run(Args(host["cfg"], host["state"]))
    assert json.loads(hb.read_text())["status"] == "down"


def test_shipped_configs_match_the_post_migration_tailnet_topology():
    src = chk.load_config(WATCHDOG / "hosts" / "src.json")
    tmn = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    observer = chk.load_config(WATCHDOG / "hosts" / "tmn-observer.json")

    assert src["host_label"] == "src"
    assert not src.get("peer") and not src.get("peers")
    assert tmn["host_label"] == "neb-ops-gcp"
    assert tmn["hermes_home"] == "~/.hermes"
    assert tmn["gateway_unit"] == "hermes-gateway.service"
    assert observer["host_label"] == "tmn-codex-observer"

    # TMN watches the source host plus its dedicated observer. The retired
    # hermes-tmn VM is no longer a monitoring target.
    assert [p["label"] for p in tmn["peers"]] == ["src", "tmn-codex-observer"]
    assert "peer" not in tmn
    assert [p["label"] for p in observer["peers"]] == ["neb-ops-gcp"]

    # Tailnet addresses only. A public URL recreates the retired Hostinger route
    # that triggered the false alert after the August migration.
    everything = list(tmn["peers"]) + list(observer["peers"])
    for peer in everything:
        assert "://100." in peer["url"], peer["url"]
        assert peer["stale_after_s"] >= 6 * 3600

    src_urls = {p["url"] for p in everything if p["label"] == "src"}
    assert src_urls == {"http://100.70.49.54:8299/heartbeat"}


def test_live_tmn_config_does_not_assert_timers_on_the_retired_vm():
    """The TMN watchdog moved to neb-ops-gcp; its sibling timers did not."""
    tmn = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    assert not tmn.get("sibling_timers")

    host = chk.load_config(WATCHDOG / "hosts" / "src.json")
    assert not host.get("sibling_timers")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def test_corrupt_state_is_disarmed_not_reset_to_ok(tmp_path):
    """Resetting to ok would turn a live outage into a fabricated recovery."""
    p = tmp_path / "state.json"
    p.write_text("{corrupt")
    with pytest.raises(chk.Disarmed, match="corrupt"):
        chk.load_state(p)


def test_absent_state_starts_clean(tmp_path):
    assert chk.load_state(tmp_path / "none.json")["status"] == "ok"


# --------------------------------------------------------------------------
# edge-trigger state machine, end to end through run()
# --------------------------------------------------------------------------

class Args:
    def __init__(self, config, state_file, **kw):
        self.config, self.state_file = str(config), str(state_file)
        self.dry_run = kw.get("dry_run", False)
        self.force_down = kw.get("force_down", False)
        self.force_peer = kw.get("force_peer", False)
        self.no_email = kw.get("no_email", False)
        self.no_slack = kw.get("no_slack", False)
        self.no_linear = kw.get("no_linear", False)
        self.no_telegram = kw.get("no_telegram", False)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A configured host whose gateway is on codex and whose email always sends."""
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: openai-codex\n")
    (home / "auth.json").write_text(json.dumps(auth_doc()))
    cfg = write_host(tmp_path, "h1", str(home))
    monkeypatch.setattr(chk, "env_val", lambda name, hh: "key-present")
    sent = []
    monkeypatch.setattr(chk, "send_email", lambda ch, s, b, k: sent.append(b))
    return {"cfg": cfg, "home": home, "state": tmp_path / "state.json", "sent": sent}


def test_healthy_is_silent(host):
    rc = chk.run(Args(host["cfg"], host["state"]))
    assert rc == 0 and host["sent"] == []


def test_first_failure_alerts_once_then_goes_quiet(host):
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))

    assert chk.run(Args(host["cfg"], host["state"])) == 0
    assert len(host["sent"]) == 1                     # ok -> down

    assert chk.run(Args(host["cfg"], host["state"])) == 0
    assert len(host["sent"]) == 1                     # down -> down, quiet window


def test_reminder_fires_after_renotify_window(host):
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1

    st = json.loads(host["state"].read_text())
    st["last_alert"] = int(time.time()) - (24 * 3600 + 60)
    host["state"].write_text(json.dumps(st))

    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 2


def test_peer_failure_does_not_remind_after_renotify_window(host):
    chk.run(Args(host["cfg"], host["state"], force_peer=True))
    assert len(host["sent"]) == 1

    st = json.loads(host["state"].read_text())
    st["last_alert"] = int(time.time()) - (24 * 3600 + 60)
    host["state"].write_text(json.dumps(st))

    chk.run(Args(host["cfg"], host["state"], force_peer=True))
    assert len(host["sent"]) == 1


def test_peer_recovery_rearms_the_first_failure_alert(host):
    chk.run(Args(host["cfg"], host["state"], force_peer=True))
    assert len(host["sent"]) == 1

    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1
    assert json.loads(host["state"].read_text())["status"] == "ok"

    chk.run(Args(host["cfg"], host["state"], force_peer=True))
    assert len(host["sent"]) == 2


def test_recovery_is_silent_and_rearms(host):
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1

    (host["home"] / "auth.json").write_text(json.dumps(auth_doc()))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1                     # no "recovered" message
    assert json.loads(host["state"].read_text())["status"] == "ok"

    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 2                     # re-armed: alerts again


def test_alert_with_every_channel_failing_exits_nonzero(host, monkeypatch):
    """The most dangerous failure mode: an outage nobody was told about.

    The version this replaces printed the delivery error and still exited 0, so
    systemd stayed green while the page went nowhere.
    """
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    monkeypatch.setattr(chk, "env_val", lambda name, hh: "")   # secret rotated away
    assert chk.run(Args(host["cfg"], host["state"])) == 1




def test_quota_outage_alerts_once_then_goes_quiet(host):
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(pool=[pool_entry()])))

    assert chk.run(Args(host["cfg"], host["state"])) == 0
    assert len(host["sent"]) == 1
    assert "out of Codex quota" in host["sent"][0]
    assert json.loads(host["state"].read_text())["status"] == "quota"

    assert chk.run(Args(host["cfg"], host["state"])) == 0
    assert len(host["sent"]) == 1                     # quiet window holds


def test_quota_reminder_still_fires_after_renotify_window(host):
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(pool=[pool_entry()])))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1

    st = json.loads(host["state"].read_text())
    st["last_alert"] = int(time.time()) - (24 * 3600 + 60)
    host["state"].write_text(json.dumps(st))

    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 2


def test_failure_kind_change_re_alerts_inside_the_quiet_window(host):
    """An auth outage that becomes a quota outage must page again.

    Inheriting the earlier alert's silence would leave "go re-login" standing as
    the last instruction given, for a problem a re-login cannot fix.
    """
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(refresh=None)))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1
    assert "can no longer sign in" in host["sent"][0]

    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(pool=[pool_entry()])))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 2                     # not silenced by the window
    assert "out of Codex quota" in host["sent"][1]
    assert "NOT a sign-in problem" in host["sent"][1]


def test_quota_recovery_is_silent_and_rearms(host):
    (host["home"] / "auth.json").write_text(json.dumps(auth_doc(pool=[pool_entry()])))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1

    (host["home"] / "auth.json").write_text(json.dumps(
        auth_doc(pool=[pool_entry(reset_offset=-60)])))
    chk.run(Args(host["cfg"], host["state"]))
    assert len(host["sent"]) == 1                     # no "recovered" message
    assert json.loads(host["state"].read_text())["status"] == "ok"


def test_dry_run_never_writes_state(host):
    before = host["state"].exists()
    chk.run(Args(host["cfg"], host["state"], dry_run=True, force_down=True))
    assert host["state"].exists() == before
    assert host["sent"] == []


def test_force_down_respects_state_file_override(host, tmp_path):
    """Drills must not be able to write production state.

    Without this, a --force-down drill leaves the host inside a 24h quiet window
    and a genuine outage in that window pages nobody.
    """
    prod = tmp_path / "prod-state.json"
    prod.write_text(json.dumps({"status": "ok", "last_alert": 0, "ticket_url": None}))
    drill = tmp_path / "drill.json"

    chk.run(Args(host["cfg"], drill, force_down=True))

    assert json.loads(prod.read_text())["status"] == "ok"
    assert json.loads(drill.read_text())["status"] == "down"


# --------------------------------------------------------------------------
# applicability guard
# --------------------------------------------------------------------------

def test_non_codex_gateway_is_quiet_but_unreadable_config_is_loud(tmp_path):
    """"Not applicable" and "cannot tell" must not look the same.

    Collapsing them meant a YAML typo silently disarmed the watchdog.
    """
    yaml = tmp_path / "config.yaml"
    yaml.write_text("model:\n  provider: anthropic\n")
    assert chk.gateway_uses_codex(yaml) is False

    with pytest.raises(chk.Disarmed):
        chk.gateway_uses_codex(tmp_path / "absent.yaml")


# --------------------------------------------------------------------------
# independent observer for TMN's Hermes heartbeat
# --------------------------------------------------------------------------

def obs_cfg(**over):
    cfg = {"host_label": "tmn-codex-observer", "mode": "observer",
           "peers": [
               {"label": "src", "bot_label": "@Screddy_bot",
                "url": "http://100.70.49.54:8299/heartbeat", "stale_after_s": 46800},
               {"label": "neb-ops-gcp", "bot_label": "@Teamnebula_bot",
                "url": "http://100.74.25.61:8299/heartbeat", "stale_after_s": 46800},
           ],
           "channels": {"telegram": {"chat_id": "1", "token_env": "TELEGRAM_BOT_TOKEN"}}}
    cfg.update(over)
    return cfg


def peers_aged(monkeypatch, ages):
    """ages maps peer label -> heartbeat age in seconds (None = unreachable)."""
    import contextlib, io, json as _json
    by_url = {"http://100.70.49.54:8299/heartbeat": "src",
              "http://100.74.25.61:8299/heartbeat": "neb-ops-gcp"}

    def opener(url, timeout=0):
        age = ages[by_url[url]]
        if age is None:
            raise OSError("unreachable")
        body = _json.dumps({"host": by_url[url], "at": int(time.time()) - age}).encode()
        return contextlib.closing(io.BytesIO(body))
    monkeypatch.setattr(chk.urllib.request, "urlopen", opener)


@pytest.mark.parametrize(
    ("ages", "expected_status", "expected_dark"),
    [
        ({"neb-ops-gcp": 60}, "ok", None),
        ({"neb-ops-gcp": 99 * 3600}, "peer", "neb-ops-gcp"),
    ],
)
def test_observer_alerts_for_dark_peers_without_live_coverage(
    monkeypatch, ages, expected_status, expected_dark
):
    """The dedicated TMN observer evaluates only the TMN heartbeat."""
    peers_aged(monkeypatch, ages)
    cfg = chk.load_config(WATCHDOG / "hosts" / "tmn-observer.json")

    status, detail, _ = chk.observe_peers(cfg, {})

    assert status == expected_status
    if expected_dark:
        assert expected_dark in detail


def test_observer_alerts_when_every_box_is_dark(monkeypatch):
    peers_aged(monkeypatch, {"src": 99 * 3600, "neb-ops-gcp": 99 * 3600})
    status, detail, _ = chk.observe_peers(obs_cfg(), {})
    assert status == "peer"
    assert "every watched box is dark" in detail
    assert "src" in detail and "neb-ops-gcp" in detail


def test_observer_unreachable_peers_still_need_two_misses(monkeypatch):
    peers_aged(monkeypatch, {"src": None, "neb-ops-gcp": None})

    status, _, fails = chk.observe_peers(obs_cfg(), {})
    assert status == "ok"                      # first miss on each, not evidence
    assert fails == {"src": 1, "neb-ops-gcp": 1}

    status, _, fails = chk.observe_peers(obs_cfg(), fails)
    assert status == "peer"
    assert fails == {"src": 2, "neb-ops-gcp": 2}


def test_observer_config_needs_no_auth_store_but_others_still_do(tmp_path):
    """Observer mode must not become a back door around the hermes_home rule."""
    p = tmp_path / "obs.json"
    p.write_text(json.dumps(obs_cfg()))
    cfg = chk.load_config(p)                    # no hermes_home, and that is fine
    assert cfg["mode"] == "observer"

    p.write_text(json.dumps(obs_cfg(mode=None)))
    with pytest.raises(chk.Disarmed, match="hermes_home"):
        chk.load_config(p)

    p.write_text(json.dumps(obs_cfg(peers=[])))
    with pytest.raises(chk.Disarmed, match="watches no peers"):
        chk.load_config(p)


@pytest.mark.parametrize(
    "covered_by",
    ["neb-ops-gcp", ["missing-peer"], ["src"]],
)
def test_observer_rejects_malformed_or_unknown_peer_coverage(tmp_path, covered_by):
    cfg = obs_cfg()
    cfg["peers"][0]["covered_by"] = covered_by
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(cfg))

    with pytest.raises(chk.Disarmed, match="covered_by"):
        chk.load_config(path)


def test_telegram_token_falls_back_to_private_hermes_store(tmp_path, monkeypatch):
    hermes_home = tmp_path / "watchdog"
    hermes_home.mkdir()
    private_home = tmp_path / "home"
    hermes = private_home / ".hermes"
    hermes.mkdir(parents=True)
    store = hermes / "llm-failback.json"
    store.write_text(json.dumps({"telegram": {"bot_token": "test-token"}}))
    store.chmod(0o600)
    monkeypatch.setattr(chk, "HOME", private_home)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    assert chk.env_val("TELEGRAM_BOT_TOKEN", hermes_home) == "test-token"

    store.chmod(0o644)
    assert chk.env_val("TELEGRAM_BOT_TOKEN", hermes_home) == ""


def test_shipped_tmn_observer_config_watches_only_tmn_over_the_tailnet():
    cfg = chk.load_config(WATCHDOG / "hosts" / "tmn-observer.json")
    assert cfg["mode"] == "observer"
    assert [p["label"] for p in cfg["peers"]] == ["neb-ops-gcp"]
    for peer in cfg["peers"]:
        assert "://100." in peer["url"]
    # The TMN-only observer reaches Shawn through Telegram and keeps company
    # credentials off the independent R2H host.
    assert sorted(cfg["channels"]) == ["telegram"]
    assert "email" not in cfg["channels"]
    assert "hermes_home" not in cfg


def test_observer_run_does_not_require_an_auth_store(tmp_path, monkeypatch):
    """Regression: run() resolved hermes_home before branching on mode.

    load_config accepted the observer config and then run() raised KeyError on
    cfg["hermes_home"]. Caught by install.sh's dry-run assertion on 2026-08-17,
    which is the only reason it did not ship.
    """
    peers_aged(monkeypatch, {"src": 60, "neb-ops-gcp": 60})
    cfg_path = tmp_path / "obs.json"
    cfg_path.write_text(json.dumps(obs_cfg()))
    sent = []
    monkeypatch.setattr(chk, "telegram_post", lambda ch, text, tok: sent.append(text))
    monkeypatch.setattr(chk, "env_val", lambda name, hh: "tok")

    rc = chk.run(Args(cfg_path, tmp_path / "state.json"))
    assert rc == 0 and sent == []               # both peers fresh, stays quiet


def test_observer_alert_reaches_telegram(tmp_path, monkeypatch):
    peers_aged(monkeypatch, {"src": 99 * 3600, "neb-ops-gcp": 99 * 3600})
    cfg_path = tmp_path / "obs.json"
    cfg_path.write_text(json.dumps(obs_cfg()))
    sent = []
    monkeypatch.setattr(chk, "telegram_post", lambda ch, text, tok: sent.append(text))
    monkeypatch.setattr(chk, "env_val", lambda name, hh: "tok")

    assert chk.run(Args(cfg_path, tmp_path / "state.json")) == 0
    assert len(sent) == 1
    assert "every watched box is dark" in sent[0]


# --------------------------------------------------------------------------
# sibling timers — the watchdog next door
# --------------------------------------------------------------------------

def fake_systemctl(monkeypatch, *, enabled="enabled", next_elapse="Tue 2026-08-18 03:00:00 UTC",
                   last="Mon 2026-08-17 21:00:00 UTC"):
    class R:
        def __init__(self, out): self.stdout = out
    def run(cmd, **kw):
        if "is-enabled" in cmd: return R(enabled + "\n")
        if "NextElapseUSecRealtime" in cmd: return R(next_elapse + "\n")
        if "LastTriggerUSec" in cmd: return R(last + "\n")
        return R("")
    monkeypatch.setattr(chk.subprocess, "run", run)


def test_armed_sibling_timer_is_quiet(monkeypatch):
    fake_systemctl(monkeypatch)
    status, detail = chk.sibling_timers_ok({"sibling_timers": ["nebos-claude-health.timer"]})
    assert status == "ok" and "armed" in detail


def test_disabled_sibling_timer_alerts(monkeypatch):
    """The 2026-08-04 failure mode: a timer switched off, nothing noticing."""
    fake_systemctl(monkeypatch, enabled="disabled")
    status, detail = chk.sibling_timers_ok({"sibling_timers": ["nebos-claude-health.timer"]})
    assert status == "sibling"
    assert "nebos-claude-health.timer is disabled" in detail
    assert "unmonitored" in detail


def test_enabled_but_unscheduled_sibling_alerts(monkeypatch):
    """Enabled with no next elapse is the silent-never-fires case."""
    fake_systemctl(monkeypatch, next_elapse="")
    status, detail = chk.sibling_timers_ok({"sibling_timers": ["t.timer"]})
    assert status == "sibling" and "no next run scheduled" in detail


def test_never_triggered_sibling_is_a_fresh_install_not_a_fault(monkeypatch):
    fake_systemctl(monkeypatch, last="n/a")
    status, _ = chk.sibling_timers_ok({"sibling_timers": ["t.timer"]})
    assert status == "ok"


def test_uncheckable_sibling_never_pages(monkeypatch):
    """A systemctl hiccup is not evidence a timer stopped."""
    def boom(*a, **k): raise OSError("systemctl gone")
    monkeypatch.setattr(chk.subprocess, "run", boom)
    status, detail = chk.sibling_timers_ok({"sibling_timers": ["t.timer"]})
    assert status == "ok" and "uncheckable" in detail


def test_nonzero_systemctl_result_never_pages_for_sibling(monkeypatch):
    """A lost user-systemd session returns nonzero rather than raising."""
    class R:
        returncode = 1
        stdout = ""
        stderr = "Failed to connect to bus: No medium found\n"

    monkeypatch.setattr(chk.subprocess, "run", lambda *a, **k: R())

    status, detail = chk.sibling_timers_ok({"sibling_timers": ["t.timer"]})

    assert status == "ok"
    assert "uncheckable" in detail


def test_no_sibling_timers_configured_is_silent():
    assert chk.sibling_timers_ok({}) == ("ok", "")


def test_sibling_alert_points_at_the_timer_not_the_credential():
    cfg = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    body = chk.alert_text(cfg, "nebos-claude-health.timer is disabled", None, "sibling")
    assert "auth.openai.com/codex/device" not in body
    assert "Nothing is wrong with this box's own credential" in body
    assert "systemctl --user enable --now" in body
    assert "stopped running" in chk.subject(cfg, "sibling")


# --------------------------------------------------------------------------
# multi-peer ring
# --------------------------------------------------------------------------

def test_any_dark_peer_is_reported_not_just_all(monkeypatch):
    """A normal watchdog reports any dark peer without applying observer coverage."""
    import contextlib, io, json as _json
    def opener(url, timeout=0):
        if "100.85.162.108" in url:                     # the TMN observer is dark
            raise OSError("unreachable")
        body = _json.dumps({"at": int(time.time()) - 60}).encode()
        return contextlib.closing(io.BytesIO(body))
    monkeypatch.setattr(chk.urllib.request, "urlopen", opener)

    cfg = chk.load_config(WATCHDOG / "hosts" / "tmn.json")
    status, _, fails = chk.read_peers(cfg, {})
    assert status == "ok" and fails["tmn-codex-observer"] == 1  # one miss, no page

    status, detail, fails = chk.read_peers(cfg, fails)
    assert status == "peer"
    assert "tmn-codex-observer" in detail
    assert fails["src"] == 0                          # healthy peer stays reset


def test_single_peer_config_still_works(monkeypatch):
    """The legacy singular form remains supported for downstream configs."""
    import contextlib, io, json as _json
    monkeypatch.setattr(chk.urllib.request, "urlopen",
                        lambda url, timeout=0: contextlib.closing(
                            io.BytesIO(_json.dumps({"at": int(time.time()) - 60}).encode())))
    cfg = chk.load_config(WATCHDOG / "hosts" / "src.json")
    cfg["peer"] = {"label": "peer", "bot_label": "bot",
                   "url": "http://100.64.0.1:8299/heartbeat", "stale_after_s": 46800}
    status, _, _ = chk.read_peers(cfg, {})
    assert status == "ok"
