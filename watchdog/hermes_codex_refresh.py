#!/usr/bin/env python3
"""Refresh one pinned Hermes Codex OAuth lineage without starting an agent."""
from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import fcntl
import hashlib
import importlib.metadata
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone


EXPECTED_AUTH_CONSTANTS = {
    "AUTH_STORE_VERSION": 1,
    "AUTH_LOCK_TIMEOUT_SECONDS": 15.0,
    "CODEX_OAUTH_CLIENT_ID": "app_EMoamEEZ73f0CkXaXp7hrann",
    "CODEX_OAUTH_TOKEN_URL": "https://auth.openai.com/oauth/token",
    "CODEX_RATE_LIMITED_CODE": "codex_rate_limited",
}


class RefreshError(Exception):
    """The helper cannot cross the refresh boundary safely."""


class UncertainRefresh(RefreshError):
    """The refresh token may have been consumed, so retrying is unsafe."""


TERMINAL_CODES = frozenset({
    "dead",
    "invalid_grant",
    "invalid_token",
    "refresh_token_reused",
    "token_invalidated",
    "token_revoked",
})


@dataclasses.dataclass(frozen=True)
class Lineage:
    name: str
    refresh_token: str
    access_token: str
    pool_index: int | None
    sync_singleton: bool

    @property
    def refresh_fingerprint(self) -> str:
        return hashlib.sha256(self.refresh_token.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


@dataclasses.dataclass(frozen=True)
class RefreshOutcome:
    status: str
    reset_at: float | None = None


def _regular_file(value: str, label: str) -> pathlib.Path:
    path = pathlib.Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RefreshError(f"{label} must be an absolute path")
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise RefreshError(f"{label} is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise RefreshError(f"{label} must be a regular file")
    return path


def _validate_hash(path: pathlib.Path, expected: str, label: str) -> None:
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise RefreshError(f"{label} SHA-256 is invalid")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RefreshError(f"{label} SHA-256 mismatch")


def _function_signature(tree: ast.AST, name: str) -> tuple[list[str], list[str]]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            positional = [argument.arg for argument in node.args.args]
            keyword_only = [argument.arg for argument in node.args.kwonlyargs]
            return positional, keyword_only
    raise RefreshError(f"Hermes contract is missing {name}")


def _literal_assignments(tree: ast.Module) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            values[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
    return values


def _validate_auth_ast(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RefreshError("Hermes auth module is not valid Python") from exc
    assignments = _literal_assignments(tree)
    for name, expected in EXPECTED_AUTH_CONSTANTS.items():
        if assignments.get(name) != expected:
            raise RefreshError(f"Hermes auth contract mismatch for {name}")
    signatures = {
        "_auth_lock_path": ([], []),
        "_auth_store_lock": (["timeout_seconds"], []),
        "_load_auth_store": (["auth_file"], []),
        "_save_auth_store": (["auth_store"], []),
        "refresh_codex_oauth_pure": (
            ["access_token", "refresh_token"],
            ["timeout_seconds"],
        ),
    }
    for name, expected in signatures.items():
        if _function_signature(tree, name) != expected:
            raise RefreshError(f"Hermes auth signature mismatch for {name}")


def _validate_pool_ast(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RefreshError("Hermes credential-pool module is not valid Python") from exc
    expected = {
        "_sync_device_code_entry_to_auth_store": (["self", "entry"], []),
        "_refresh_entry": (["self", "entry"], ["force"]),
    }
    for name, signature in expected.items():
        if _function_signature(tree, name) != signature:
            raise RefreshError(f"Hermes credential-pool signature mismatch for {name}")


def validate_contract(args: argparse.Namespace) -> None:
    if os.path.abspath(sys.executable) != os.path.abspath(args.expected_python):
        raise RefreshError("Hermes Python executable mismatch")
    try:
        installed_version = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RefreshError("Hermes package metadata is unavailable") from exc
    if installed_version != args.expected_version:
        raise RefreshError("Hermes version mismatch")

    validate_source_contract(
        args.auth_module,
        args.auth_sha256,
        args.pool_module,
        args.pool_sha256,
    )


def validate_source_contract(
    auth_module_value: str,
    auth_sha256: str,
    pool_module_value: str,
    pool_sha256: str,
) -> None:
    auth_module = _regular_file(auth_module_value, "Hermes auth module")
    pool_module = _regular_file(
        pool_module_value, "Hermes credential-pool module"
    )
    _validate_hash(auth_module, auth_sha256, "Hermes auth module")
    _validate_hash(pool_module, pool_sha256, "Hermes credential-pool module")
    _validate_auth_ast(auth_module.read_text(encoding="utf-8"))
    _validate_pool_ast(pool_module.read_text(encoding="utf-8"))


def _read_auth(auth_path: pathlib.Path) -> dict:
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RefreshError("Hermes auth state is unreadable") from exc
    if not isinstance(auth, dict):
        raise RefreshError("Hermes auth state is malformed")
    providers = auth.get("providers", {})
    pool = auth.get("credential_pool", {})
    if not isinstance(providers, dict) or not isinstance(pool, dict):
        raise RefreshError("Hermes auth state is malformed")
    return auth


def _terminal(entry: dict) -> bool:
    status = str(entry.get("last_status") or "").strip().lower()
    error = entry.get("last_auth_error") or {}
    if not isinstance(error, dict):
        raise RefreshError("Hermes credential error state is malformed")
    codes = (
        entry.get("last_error_code"),
        entry.get("last_error_reason"),
        error.get("code"),
    )
    return status in TERMINAL_CODES or any(
        str(code or "").strip().lower() in TERMINAL_CODES for code in codes
    )


def select_lineage(auth: dict) -> Lineage:
    pool = auth.get("credential_pool", {}).get("openai-codex", [])
    if pool:
        if not isinstance(pool, list) or any(not isinstance(entry, dict) for entry in pool):
            raise RefreshError("Codex credential pool is malformed")
        ids = [entry.get("id") for entry in pool]
        if any(not isinstance(entry_id, str) or not entry_id for entry_id in ids):
            raise RefreshError("Codex credential pool has an invalid entry ID")
        if len(set(ids)) != len(ids):
            raise RefreshError("Codex credential pool has ambiguous entry IDs")

        eligible: list[tuple[int, int, dict]] = []
        for index, entry in enumerate(pool):
            priority = entry.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise RefreshError("Codex credential pool has an invalid priority")
            if _terminal(entry):
                continue
            if entry.get("auth_type") != "oauth":
                continue
            source = entry.get("source")
            if source != "device_code" and not (
                isinstance(source, str)
                and (source == "manual" or source.startswith("manual:"))
            ):
                raise RefreshError("Codex credential source is unsupported")
            access_token = entry.get("access_token")
            refresh_token = entry.get("refresh_token")
            if not isinstance(access_token, str) or not isinstance(refresh_token, str):
                raise RefreshError("Codex credential tokens are malformed")
            if not refresh_token:
                continue
            eligible.append((priority, index, entry))
        if not eligible:
            raise RefreshError("no eligible Codex credential lineage")
        eligible.sort(key=lambda item: item[0])
        if len(eligible) > 1 and eligible[0][0] == eligible[1][0]:
            raise RefreshError("Codex credential selection is ambiguous")
        _, index, selected = eligible[0]
        sync_singleton = selected.get("source") == "device_code"
        if sync_singleton:
            provider = auth.get("providers", {}).get("openai-codex")
            tokens = provider.get("tokens") if isinstance(provider, dict) else None
            if not isinstance(tokens, dict):
                raise RefreshError("device-code pool lineage has no singleton")
            if (
                tokens.get("access_token") != selected["access_token"]
                or tokens.get("refresh_token") != selected["refresh_token"]
            ):
                raise RefreshError("device-code singleton and pool lineage diverged")
        return Lineage(
            name=f"pool:{selected['id']}",
            refresh_token=selected["refresh_token"],
            access_token=selected["access_token"],
            pool_index=index,
            sync_singleton=sync_singleton,
        )

    provider = auth.get("providers", {}).get("openai-codex")
    if not isinstance(provider, dict) or _terminal(provider):
        raise RefreshError("no eligible Codex credential lineage")
    tokens = provider.get("tokens")
    if not isinstance(tokens, dict):
        raise RefreshError("Codex singleton tokens are malformed")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise RefreshError("Codex singleton tokens are malformed")
    if not refresh_token:
        raise RefreshError("no eligible Codex credential lineage")
    return Lineage(
        name="singleton",
        refresh_token=refresh_token,
        access_token=access_token,
        pool_index=None,
        sync_singleton=True,
    )


def _default_transport(
    endpoint: str, fields: dict[str, str], timeout: float
) -> HttpResult:
    if endpoint != EXPECTED_AUTH_CONSTANTS["CODEX_OAUTH_TOKEN_URL"]:
        raise RefreshError("Codex token endpoint is not pinned")
    encoded = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        endpoint,
        data=encoded,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(
                int(response.status),
                response.read(64 * 1024 + 1),
                dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            int(exc.code),
            exc.read(64 * 1024 + 1),
            dict(exc.headers.items()) if exc.headers else {},
        )
    except Exception as exc:
        raise UncertainRefresh("Codex refresh response is uncertain") from exc


def _json_body(result: HttpResult) -> dict:
    if len(result.body) > 64 * 1024:
        raise UncertainRefresh("Codex refresh response exceeded the safe limit")
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UncertainRefresh("Codex refresh response was malformed") from exc
    if not isinstance(payload, dict):
        raise UncertainRefresh("Codex refresh response was malformed")
    return payload


def _find_reset_at(value: object) -> float | None:
    if isinstance(value, dict):
        candidate = value.get("resets_at")
        if (
            not isinstance(candidate, bool)
            and isinstance(candidate, (int, float))
            and float(candidate) > 0
        ):
            return float(candidate)
        for nested in value.values():
            found = _find_reset_at(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_reset_at(nested)
            if found is not None:
                return found
    return None


def _quota_reset(result: HttpResult, payload: dict, now: float) -> float | None:
    reset_at = _find_reset_at(payload)
    if reset_at is not None:
        return reset_at
    retry_after = next(
        (
            value for key, value in result.headers.items()
            if key.lower() == "retry-after"
        ),
        None,
    )
    if retry_after is None:
        return None
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        return None
    return now + delay if delay > 0 else None


def _clear_pool_errors(entry: dict) -> None:
    entry["last_status"] = "ok"
    entry["last_status_at"] = None
    entry["last_error_code"] = None
    entry["last_error_reason"] = None
    entry["last_error_message"] = None
    entry["last_error_reset_at"] = None


def _apply_tokens(
    auth: dict,
    lineage: Lineage,
    access_token: str,
    refresh_token: str,
    last_refresh: str,
) -> None:
    if lineage.pool_index is not None:
        entries = auth["credential_pool"]["openai-codex"]
        entry = entries[lineage.pool_index]
        if f"pool:{entry.get('id')}" != lineage.name:
            raise UncertainRefresh("selected Codex lineage changed before persistence")
        entry["access_token"] = access_token
        entry["refresh_token"] = refresh_token
        entry["last_refresh"] = last_refresh
        _clear_pool_errors(entry)
        if not lineage.sync_singleton:
            return
    provider = auth["providers"]["openai-codex"]
    tokens = provider["tokens"]
    tokens["access_token"] = access_token
    tokens["refresh_token"] = refresh_token
    provider["last_refresh"] = last_refresh


def _atomic_write_auth(auth_path: pathlib.Path, auth: dict, now: float) -> None:
    auth["version"] = EXPECTED_AUTH_CONSTANTS["AUTH_STORE_VERSION"]
    auth["updated_at"] = datetime.fromtimestamp(
        now, tz=timezone.utc
    ).isoformat()
    payload = (json.dumps(auth, indent=2) + "\n").encode("utf-8")
    temp_path = auth_path.with_name(
        f"{auth_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    fd = -1
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, auth_path)
        os.chmod(auth_path, 0o600)
        directory_fd = os.open(auth_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_persisted(
    auth_path: pathlib.Path,
    lineage_name: str,
    access_token: str,
    refresh_token: str,
) -> None:
    persisted = _read_auth(auth_path)
    lineage = select_lineage(persisted)
    if lineage.name != lineage_name:
        raise UncertainRefresh("persisted Codex lineage changed")
    if lineage.access_token != access_token or lineage.refresh_token != refresh_token:
        raise UncertainRefresh("persisted Codex lineage failed verification")
    if lineage.sync_singleton and lineage.pool_index is not None:
        tokens = persisted["providers"]["openai-codex"]["tokens"]
        if (
            tokens.get("access_token") != access_token
            or tokens.get("refresh_token") != refresh_token
        ):
            raise UncertainRefresh("persisted Codex singleton failed verification")


def refresh_auth_store(
    auth_path: pathlib.Path,
    *,
    expected_lineage: str,
    expected_fingerprint: str,
    transport=_default_transport,
    now: float | None = None,
    lock_timeout: float = 5.0,
) -> RefreshOutcome:
    auth_path = pathlib.Path(auth_path)
    now = time.time() if now is None else float(now)
    with auth_lock(auth_path, lock_timeout):
        auth = _read_auth(auth_path)
        lineage = select_lineage(auth)
        if (
            lineage.name != expected_lineage
            or lineage.refresh_fingerprint != expected_fingerprint
        ):
            raise RefreshError("Codex credential lineage changed before request")

        fields = {
            "grant_type": "refresh_token",
            "refresh_token": lineage.refresh_token,
            "client_id": str(EXPECTED_AUTH_CONSTANTS["CODEX_OAUTH_CLIENT_ID"]),
        }
        try:
            result = transport(
                str(EXPECTED_AUTH_CONSTANTS["CODEX_OAUTH_TOKEN_URL"]),
                fields,
                20.0,
            )
        except UncertainRefresh:
            raise
        except Exception as exc:
            raise UncertainRefresh("Codex refresh response is uncertain") from exc
        if not isinstance(result, HttpResult):
            raise UncertainRefresh("Codex refresh transport returned malformed state")
        payload = _json_body(result)
        if result.status == 429:
            return RefreshOutcome("quota", _quota_reset(result, payload, now))
        if result.status != 200:
            raise RefreshError(f"Codex refresh failed with HTTP {result.status}")

        access_token = payload.get("access_token")
        rotated_refresh = payload.get("refresh_token", lineage.refresh_token)
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or not isinstance(rotated_refresh, str)
            or not rotated_refresh.strip()
        ):
            raise UncertainRefresh("Codex refresh response omitted rotated tokens")
        access_token = access_token.strip()
        rotated_refresh = rotated_refresh.strip()
        last_refresh = datetime.fromtimestamp(
            now, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        _apply_tokens(
            auth,
            lineage,
            access_token,
            rotated_refresh,
            last_refresh,
        )
        _atomic_write_auth(auth_path, auth, now)
        _verify_persisted(
            auth_path,
            lineage.name,
            access_token,
            rotated_refresh,
        )
        return RefreshOutcome("persisted")


@contextlib.contextmanager
def auth_lock(auth_path: pathlib.Path, timeout_seconds: float):
    lock_path = auth_path.with_suffix(".lock")
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    lock_file = os.fdopen(fd, "a+")
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RefreshError("timed out waiting for Hermes auth lock")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-readiness", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--auth-json")
    parser.add_argument("--lineage")
    parser.add_argument("--refresh-fingerprint")
    parser.add_argument("--lock-timeout", type=float, default=5.0)
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--auth-module", required=True)
    parser.add_argument("--auth-sha256", required=True)
    parser.add_argument("--pool-module", required=True)
    parser.add_argument("--pool-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_contract(args)
        if args.check_readiness:
            print(json.dumps({"status": "ready"}, separators=(",", ":")))
            return 0
        if args.plan:
            if not args.auth_json:
                raise RefreshError("plan requires an auth store")
            auth_path = _regular_file(args.auth_json, "Hermes auth store")
            with auth_lock(auth_path, args.lock_timeout):
                lineage = select_lineage(_read_auth(auth_path))
            print(json.dumps({
                "status": "planned",
                "lineage": lineage.name,
                "refresh_fingerprint": lineage.refresh_fingerprint,
            }, separators=(",", ":")))
            return 0
        if args.refresh:
            if not args.auth_json or not args.lineage or not args.refresh_fingerprint:
                raise RefreshError("refresh requires auth store and planned lineage")
            auth_path = _regular_file(args.auth_json, "Hermes auth store")
            outcome = refresh_auth_store(
                auth_path,
                expected_lineage=args.lineage,
                expected_fingerprint=args.refresh_fingerprint,
                lock_timeout=args.lock_timeout,
            )
            payload = {"status": outcome.status}
            if outcome.reset_at is not None:
                payload["reset_at"] = outcome.reset_at
            print(json.dumps(payload, separators=(",", ":")))
            return 3 if outcome.status == "quota" else 0
        if not args.check_readiness:
            raise RefreshError("no helper action selected")
    except UncertainRefresh as exc:
        print(
            json.dumps(
                {"status": "uncertain", "detail": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 4
    except RefreshError as exc:
        print(
            json.dumps(
                {"status": "disarmed", "detail": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
