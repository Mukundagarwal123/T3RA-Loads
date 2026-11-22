"""Shared HTTP behaviour for the outbound API clients.

Provides a retrying request helper and a small token cache. Retries use
exponential backoff with jitter and apply only to transient failures --
connection errors, timeouts, 429 and 5xx. A 4xx other than 429 is a
caller error and is raised immediately rather than hammered.

Authentication failures are special: 401/403/423 are NEVER retried, and a
failed credential exchange is *latched* by ``TokenCache`` so the process
stops re-presenting bad credentials. Upstreams such as Turvo lock the
account out after a handful of consecutive failed logins, so a retry here
does not recover the request -- it destroys access for everyone. Clearing
the latch is a deliberate act (``TokenCache.clear()``), not a backoff.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Never retried, at any layer: re-sending rejected credentials is what
#: trips upstream account lockouts. 423 is the lockout itself.
AUTH_FAILURE_STATUS = frozenset({401, 403, 423})


class AuthFailedError(RuntimeError):
    """An upstream rejected our credentials. Terminal -- do not retry.

    Raised instead of ``requests.HTTPError`` for 401/403/423 so callers can
    distinguish "we are not allowed in" from "the request went wrong".
    """

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(
            f"Authentication rejected with HTTP {status_code} for {url}; "
            "not retrying (further attempts risk an upstream account lockout)"
        )
        self.status_code = status_code
        self.url = url


class TokenCache:
    """Thread-safe cache for a bearer token with an expiry."""

    def __init__(self, *, refresh_margin_seconds: int = 60) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._margin = refresh_margin_seconds
        self._failure: BaseException | None = None

    def get(self, fetch: Callable[[], tuple[str, float]]) -> str:
        """Return a live token, calling ``fetch`` only when the cached one is stale.

        ``fetch`` returns ``(token, ttl_seconds)``.

        A failed ``fetch`` is latched: the same error is re-raised on every
        later call without contacting the upstream again. Callers loop over
        many records, so without the latch one bad credential becomes one
        failed login per record -- which is precisely how an account gets
        locked out. ``clear()`` re-arms it.
        """
        now = time.time()
        with self._lock:
            if self._failure is not None:
                raise self._failure

            if self._token and now < (self._expires_at - self._margin):
                return self._token

            try:
                token, ttl_seconds = fetch()
            except BaseException as exc:
                self._failure = exc
                self._token = None
                self._expires_at = 0.0
                logger.error(
                    "Token fetch failed; latching failure and refusing further "
                    "credential exchanges until the cache is cleared",
                    extra={"error": repr(exc)},
                )
                raise

            self._token = token
            self._expires_at = now + float(ttl_seconds)
            return token

    @property
    def failed(self) -> bool:
        """True when a latched failure is blocking further token fetches."""
        with self._lock:
            return self._failure is not None

    def clear(self) -> None:
        """Drop the cached token and release any latched failure."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            self._failure = None


def request_with_retry(
    method: str,
    url: str,
    *,
    timeout: int,
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 20.0,
    slow_threshold_ms: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Issue an HTTP request, retrying transient failures with backoff + jitter.

    Every attempt is timed. When ``slow_threshold_ms`` is set and an attempt
    takes longer than that, it is logged as a warning -- upstream latency is
    otherwise invisible until someone notices the dashboard hanging.

    An auth rejection (401/403/423) short-circuits on the first attempt with
    :class:`AuthFailedError` and is never retried, whatever ``max_attempts``
    says. Pass ``max_attempts=1`` for credential exchanges so a transient
    429/5xx on the login endpoint cannot turn into several login attempts.

    Raises the final ``requests`` exception when every attempt fails.
    """
    last_error: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        started_at = time.perf_counter()
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            _log_duration(method, url, attempt, started_at, slow_threshold_ms, response.status_code)
            if response.status_code in AUTH_FAILURE_STATUS:
                logger.error(
                    "Upstream rejected credentials; aborting without retry",
                    extra={"url": url, "status_code": response.status_code, "attempt": attempt},
                )
                raise AuthFailedError(response.status_code, url)
            if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                logger.warning(
                    "Retrying transient HTTP status",
                    extra={"url": url, "status_code": response.status_code, "attempt": attempt},
                )
                _sleep_backoff(attempt, base_delay_seconds, max_delay_seconds)
                continue
            response.raise_for_status()
            return response
        except (requests.ConnectionError, requests.Timeout) as exc:
            _log_duration(method, url, attempt, started_at, slow_threshold_ms, None)
            last_error = exc
            if attempt >= max_attempts:
                break
            logger.warning(
                "Retrying transient HTTP error",
                extra={"url": url, "attempt": attempt, "error": repr(exc)},
            )
            _sleep_backoff(attempt, base_delay_seconds, max_delay_seconds)
        except requests.HTTPError as exc:
            # Non-retryable status, or the final attempt of a retryable one.
            raise exc

    assert last_error is not None
    raise last_error


def _log_duration(
    method: str,
    url: str,
    attempt: int,
    started_at: float,
    slow_threshold_ms: int | None,
    status_code: int | None,
) -> None:
    """Record how long one attempt took, loudly when it was slow."""
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    context = {
        "method": method,
        "url": url,
        "attempt": attempt,
        "status_code": status_code,
        "duration_ms": duration_ms,
    }
    if slow_threshold_ms is not None and duration_ms >= slow_threshold_ms:
        logger.warning("Slow upstream response", extra={**context, "slow_threshold_ms": slow_threshold_ms})
    else:
        logger.debug("Upstream response", extra=context)


def _sleep_backoff(attempt: int, base: float, cap: float) -> None:
    delay = min(cap, base * (2 ** (attempt - 1)))
    time.sleep(delay * (0.5 + random.random() / 2))
