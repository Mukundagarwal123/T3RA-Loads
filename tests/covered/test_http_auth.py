"""Auth failures must never be retried.

Regression tests for a Turvo account lockout: the carrier-contacts script
re-authenticated on every 401 inside a per-carrier loop, producing one
failed login per carrier and tripping Turvo's consecutive-failure lockout.
"""

from unittest import mock

import pytest
import requests

from t3ra_unified.clients.http import (
    AUTH_FAILURE_STATUS,
    AuthFailedError,
    TokenCache,
    request_with_retry,
)


def _response(status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://example.test/v1/oauth/token"
    return response


@pytest.mark.parametrize("status", sorted(AUTH_FAILURE_STATUS))
def test_auth_status_makes_exactly_one_attempt(status: int) -> None:
    """401/403/423 abort on the first attempt regardless of max_attempts."""
    attempts = []

    def fake_request(method, url, **kwargs):
        attempts.append(url)
        return _response(status)

    with (
        mock.patch("requests.request", side_effect=fake_request),
        pytest.raises(AuthFailedError) as excinfo,
    ):
        request_with_retry("POST", "https://example.test/x", timeout=5, max_attempts=5)

    assert len(attempts) == 1
    assert excinfo.value.status_code == status


def test_transient_status_still_retries() -> None:
    """The auth guard must not disable retries for genuinely transient codes."""
    statuses = [429, 429, 200]
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return _response(statuses[len(calls) - 1])

    with mock.patch("requests.request", side_effect=fake_request), mock.patch("time.sleep"):
        response = request_with_retry("GET", "https://example.test/x", timeout=5, max_attempts=3)

    assert len(calls) == 3
    assert response.status_code == 200


def test_token_cache_latches_failure_across_many_callers() -> None:
    """A thousand callers after a failed login must trigger one login, not a thousand."""
    logins = []

    def bad_fetch() -> tuple[str, float]:
        logins.append(1)
        raise AuthFailedError(401, "https://example.test/v1/oauth/token")

    cache = TokenCache()
    for _ in range(1000):
        with pytest.raises(AuthFailedError):
            cache.get(bad_fetch)

    assert len(logins) == 1
    assert cache.failed is True


def test_token_cache_clear_releases_the_latch() -> None:
    """Recovery is explicit: clear() allows exactly one more attempt."""
    logins = []

    def bad_fetch() -> tuple[str, float]:
        logins.append(1)
        raise AuthFailedError(401, "https://example.test/v1/oauth/token")

    cache = TokenCache()
    with pytest.raises(AuthFailedError):
        cache.get(bad_fetch)
    cache.clear()
    with pytest.raises(AuthFailedError):
        cache.get(bad_fetch)

    assert len(logins) == 2


def test_successful_token_is_still_cached() -> None:
    """The latch must not interfere with the normal caching path."""
    fetches = []

    def good_fetch() -> tuple[str, float]:
        fetches.append(1)
        return "tok", 3600.0

    cache = TokenCache()
    assert cache.get(good_fetch) == "tok"
    assert cache.get(good_fetch) == "tok"
    assert len(fetches) == 1
    assert cache.failed is False
