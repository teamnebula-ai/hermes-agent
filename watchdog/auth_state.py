"""Passive Codex credential-state parsing shared by watchdog components."""
from __future__ import annotations

import re
import time

PROVIDER = "openai-codex"
QUOTA_STALE_S = 6 * 3600
TERMINAL_STATUSES = frozenset({"dead"})


def reset_at_of(entry: dict) -> float | None:
    """When the quota window rolls, from the 429 body Hermes stored verbatim.

    The body is a Python repr of OpenAI's JSON, not JSON, so it is matched rather
    than parsed. ``resets_at`` is the only authority worth trusting here: it is
    what distinguishes "blocked right now" from "was blocked last week".
    """
    explicit = entry.get("last_error_reset_at")
    if explicit:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    m = re.search(r"['\"]resets_at['\"]\s*:\s*(\d{9,})", str(entry.get("last_error_message") or ""))
    return float(m.group(1)) if m else None


def entry_quota_blocked(entry: dict, now: float, stale_s: int) -> tuple[bool, float | None]:
    """Is this one pooled credential currently refused for quota?"""
    status = str(entry.get("last_status") or "").lower()
    code = str(entry.get("last_error_code") or "")
    msg = str(entry.get("last_error_message") or "").lower()
    looks_quota = status == "exhausted" or (
        code == "429" and ("usage_limit" in msg or "usage limit" in msg))
    if not looks_quota:
        return False, None

    reset = reset_at_of(entry)
    if reset is not None:
        return reset > now, reset

    try:
        at = float(entry.get("last_status_at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    return (now - at) <= stale_s, None


def quota_blocked(auth: dict, now: float | None = None,
                  stale_s: int = QUOTA_STALE_S) -> tuple[bool, str]:
    """True when every pooled Codex credential is refused for quota."""
    pool = (auth.get("credential_pool") or {}).get(PROVIDER) or []
    if not pool:
        return False, ""

    now = time.time() if now is None else now
    blocked = []
    for entry in pool:
        is_blocked, reset = entry_quota_blocked(entry, now, stale_s)
        if not is_blocked:
            return False, ""
        blocked.append((entry, reset))

    labels = ", ".join(str(e.get("label") or e.get("id") or "?") for e, _ in blocked)
    resets = [r for _, r in blocked if r]
    when = (time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(max(resets)))
            if resets else "unknown")
    plural = "s" if len(blocked) > 1 else ""
    return True, (f"{len(blocked)} pooled credential{plural} out of quota ({labels}); "
                  f"window resets {when}")


def renewable_pool_entries(auth: dict) -> list[dict]:
    """Pooled credentials Hermes can still refresh and route through."""
    pool = (auth.get("credential_pool") or {}).get(PROVIDER) or []
    renewable = []
    for entry in pool:
        tokens = entry.get("tokens") or entry
        if str(entry.get("last_status") or "").lower() in TERMINAL_STATUSES:
            continue
        if tokens.get("refresh_token"):
            renewable.append(entry)
    return sorted(renewable, key=lambda entry: int(entry.get("priority") or 0))


def selected_codex_credential(auth: dict, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    pool = (auth.get("credential_pool") or {}).get(PROVIDER) or []
    if pool:
        available = []
        for entry in renewable_pool_entries(auth):
            if str(entry.get("last_status") or "").lower() in TERMINAL_STATUSES:
                continue
            blocked, _ = entry_quota_blocked(entry, now, QUOTA_STALE_S)
            if not blocked:
                available.append(entry)
        if not available:
            return None
        return min(available, key=lambda entry: int(entry.get("priority", 0)))

    provider = (auth.get("providers") or {}).get(PROVIDER) or {}
    tokens = provider.get("tokens") or provider
    if not tokens.get("access_token") and not tokens.get("refresh_token"):
        return None
    return {
        "id": "singleton",
        "label": "singleton",
        "source": "device_code",
        "auth_type": "oauth",
        "access_token": tokens.get("access_token") or "",
        "refresh_token": tokens.get("refresh_token") or "",
        "last_status": provider.get("last_status"),
        "last_error_code": (provider.get("last_auth_error") or {}).get("code"),
    }


def full_pool_reset_at(auth: dict, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    pool = (auth.get("credential_pool") or {}).get(PROVIDER) or []
    if not pool:
        return None
    resets = []
    for entry in pool:
        blocked, reset = entry_quota_blocked(entry, now, QUOTA_STALE_S)
        if not blocked:
            return None
        if reset is None:
            return None
        resets.append(reset)
    return max(resets) if resets else None
