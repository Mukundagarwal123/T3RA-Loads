import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

import config

logger = logging.getLogger(__name__)

# Seconds of headroom before real expiry when deciding a cached token is stale.
_EXPIRY_SKEW_SECONDS = 60
# A login lock older than this is assumed to be from a crashed process.
_LOCK_STALE_SECONDS = 60
# How long to wait on another process's in-flight login before giving up on it.
_LOCK_WAIT_SECONDS = 15

_CACHE_PATH = Path(config.TOKEN_CACHE_PATH)
_LOCK_PATH = _CACHE_PATH.with_name(_CACHE_PATH.name + ".lock")

# Per-process mirror of the on-disk cache, so the hot path avoids a file read.
_MEM_CACHE: Dict[str, Any] = {}


class TurvoAuthError(RuntimeError):
    """Turvo rejected our credentials.

    This is never transient: retrying re-sends the same bad password, which is
    what locks the account out. Callers must treat it as fatal, not retryable.
    """


def _cache_key() -> str:
    """Bind the cache to the account it was issued for, so a credential change
    (or a sandbox/production switch) invalidates it instead of being reused."""
    return f"{config.TURVO_BASE_URL}|{config.TURVO_USERNAME}"


def _read_cache() -> Dict[str, Any]:
    if _MEM_CACHE.get("key") == _cache_key():
        return _MEM_CACHE

    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    if not isinstance(data, dict) or data.get("key") != _cache_key():
        return {}

    _MEM_CACHE.clear()
    _MEM_CACHE.update(data)
    return data


def _write_cache(data: Dict[str, Any]) -> None:
    data["key"] = _cache_key()

    _MEM_CACHE.clear()
    _MEM_CACHE.update(data)

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(_CACHE_PATH.parent), prefix=".turvo_token.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, _CACHE_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            os.chmod(_CACHE_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        # An unwritable cache path degrades us to per-process caching, which is
        # worse but still correct.
        logger.warning("Could not write token cache at %s; using in-memory cache only", _CACHE_PATH)


def _cached_token(data: Dict[str, Any]) -> Optional[str]:
    token = data.get("access_token")
    expires_at = float(data.get("expires_at") or 0.0)
    if token and time.time() < (expires_at - _EXPIRY_SKEW_SECONDS):
        return token
    return None


def _auth_cooldown_remaining(data: Dict[str, Any]) -> float:
    """Seconds left before we are allowed to attempt another login."""
    failed_at = data.get("auth_failed_at")
    if not failed_at:
        return 0.0
    return max(0.0, float(failed_at) + config.AUTH_COOLDOWN_SECONDS - time.time())


def _record_auth_failure(message: str) -> None:
    data = dict(_read_cache())
    data.pop("access_token", None)
    data.pop("expires_at", None)
    data["auth_failed_at"] = time.time()
    data["auth_error"] = message[:500]
    _write_cache(data)


def _acquire_login_lock() -> bool:
    """Best-effort cross-process lock, so a fleet restart causes one login, not N."""
    try:
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if (time.time() - _LOCK_PATH.stat().st_mtime) > _LOCK_STALE_SECONDS:
                _LOCK_PATH.unlink()
                logger.warning("Cleared stale Turvo login lock at %s", _LOCK_PATH)
        except OSError:
            pass
        return False
    except OSError:
        # Can't lock (e.g. unwritable dir); proceed unlocked rather than block.
        return True


def _release_login_lock() -> None:
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


def _refresh(refresh_token: str) -> Optional[str]:
    """Renew the access token without sending the password.

    Returns None if the refresh grant can't produce a usable token, in which
    case the caller falls back to a password login. Note that while the current
    token is still valid Turvo returns that same token with its remaining
    lifetime rather than minting a new one, so a short expires_in here is
    treated as a miss.
    """
    url = f"{config.TURVO_BASE_URL}/v1/oauth/token"
    headers = {"x-api-key": config.TURVO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "grant_type": "refresh_token",
        "client_id": config.TURVO_CLIENT_ID,
        "client_secret": config.TURVO_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "type": "business",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
    except requests.RequestException as e:
        logger.warning("Turvo token refresh failed (network): %s", e)
        return None

    if resp.status_code != 200:
        # An expired/revoked refresh token is not a password rejection, so this
        # must not trip the auth cooldown - we just fall back to logging in.
        logger.info(
            "Turvo refresh grant returned HTTP %s; falling back to password login",
            resp.status_code,
        )
        return None

    try:
        data = resp.json()
        token = data["access_token"]
        expires_in = float(data.get("expires_in", 0))
    except (ValueError, KeyError):
        logger.warning("Turvo refresh grant returned an unexpected body")
        return None

    if expires_in <= _EXPIRY_SKEW_SECONDS:
        return None

    _write_cache({
        "access_token": token,
        "expires_at": time.time() + expires_in,
        "refresh_token": data.get("refresh_token") or refresh_token,
    })
    logger.info("Turvo token refreshed without password | expires_in=%ss", int(expires_in))
    return token


def _login() -> str:
    url = f"{config.TURVO_BASE_URL}/v1/oauth/token"
    headers = {"x-api-key": config.TURVO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "grant_type": "password",
        "client_id": config.TURVO_CLIENT_ID,
        "client_secret": config.TURVO_CLIENT_SECRET,
        "username": config.TURVO_USERNAME,
        "password": config.TURVO_PASSWORD,
        "scope": "read+trust+write",
        "type": "business",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)

    # 400/401/403 from the token endpoint means the credentials themselves are
    # wrong, or the account is already locked. Never retry these.
    if resp.status_code in (400, 401, 403):
        message = (
            f"Turvo login rejected for {config.TURVO_USERNAME} "
            f"(HTTP {resp.status_code}): {resp.text[:300]}"
        )
        _record_auth_failure(message)
        logger.error("%s | suppressing further logins for %ss", message, config.AUTH_COOLDOWN_SECONDS)
        raise TurvoAuthError(message)

    resp.raise_for_status()

    data = resp.json()
    token = data["access_token"]
    expires_in = float(data.get("expires_in", 3600))

    _write_cache({
        "access_token": token,
        "expires_at": time.time() + expires_in,
        "refresh_token": data.get("refresh_token"),
    })
    logger.info("Turvo token obtained | user=%s expires_in=%ss", config.TURVO_USERNAME, int(expires_in))
    return token


def get_access_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        token = _cached_token(_read_cache())
        if token:
            return token

    cooldown = _auth_cooldown_remaining(_read_cache())
    if cooldown > 0:
        raise TurvoAuthError(
            f"Turvo credentials were rejected recently; not retrying for another "
            f"{int(cooldown)}s. Last error: {_read_cache().get('auth_error')}"
        )

    got_lock = _acquire_login_lock()
    if not got_lock:
        # Another process is logging in right now. Wait for its result rather
        # than firing a second login of our own.
        deadline = time.time() + _LOCK_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(0.5)
            _MEM_CACHE.clear()
            fresh = _read_cache()
            token = _cached_token(fresh)
            if token:
                return token
            if _auth_cooldown_remaining(fresh) > 0:
                raise TurvoAuthError(f"Concurrent Turvo login failed: {fresh.get('auth_error')}")
            if not _LOCK_PATH.exists():
                break
        got_lock = _acquire_login_lock()

    try:
        # Re-check under the lock: another process may have just written a token.
        _MEM_CACHE.clear()
        cache = _read_cache()
        if not force_refresh:
            token = _cached_token(cache)
            if token:
                return token

        # Prefer the refresh grant: it renews the token without transmitting
        # the password, so renewals can never contribute to a lockout.
        stored_refresh = cache.get("refresh_token")
        if stored_refresh:
            token = _refresh(stored_refresh)
            if token:
                return token

        return _login()
    finally:
        if got_lock:
            _release_login_lock()


def _authed_get(url: str) -> Dict[str, Any]:
    """GET with a cached token, refreshing exactly once on a 401."""
    for attempt in (1, 2):
        token = get_access_token(force_refresh=(attempt == 2))
        headers = {
            "x-api-key": config.TURVO_API_KEY,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code == 401:
            if attempt == 1:
                logger.info("Turvo returned 401; refreshing token once | url=%s", url)
                continue
            message = f"Turvo rejected a freshly issued token (HTTP 401) | url={url}"
            _record_auth_failure(message)
            raise TurvoAuthError(message)

        resp.raise_for_status()
        return resp.json()

    raise TurvoAuthError(f"Unable to authenticate against {url}")


def fetch_shipment_details(shipment_id: int) -> Dict[str, Any]:
    return _authed_get(f"{config.TURVO_BASE_URL}/v1/shipments/{shipment_id}")


def fetch_carrier_details(carrier_id: int) -> Dict[str, Any]:
    return _authed_get(f"{config.TURVO_BASE_URL}/v1/carriers/{carrier_id}")
