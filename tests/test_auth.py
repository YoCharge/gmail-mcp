import time

import httpx
import pytest

from gmail_mcp.auth import AuthError, Credentials
from conftest import make_client


def token_response(access_token="access-1", expires_in=3600):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": access_token, "expires_in": expires_in}
        )

    return handler


def test_fetches_an_access_token(credentials):
    client = make_client(token_response())
    assert credentials.access_token(client) == "access-1"


def test_caches_the_token_between_calls(credentials):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})

    client = make_client(handler)
    credentials.access_token(client)
    credentials.access_token(client)

    assert len(calls) == 1, "second call should reuse the cached token"


def test_refreshes_once_the_token_is_near_expiry(credentials):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})

    client = make_client(handler)
    credentials.access_token(client)

    # Inside the safety margin: the cached token is still technically valid but too
    # close to the boundary to hand out.
    credentials._expires_at = time.time() + 10
    credentials.access_token(client)

    assert len(calls) == 2


def test_refresh_forces_a_new_exchange_even_when_cached(credentials):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})

    client = make_client(handler)
    credentials.access_token(client)
    credentials.refresh(client)

    assert len(calls) == 2


def test_invalid_grant_explains_the_two_usual_causes(credentials):
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(AuthError) as exc:
        credentials.access_token(make_client(handler))

    message = str(exc.value)
    assert "invalid_grant" in message
    assert "Testing mode" in message, "the 7-day expiry trap should be named"


def test_a_200_without_a_token_is_still_an_error(credentials):
    def handler(request):
        return httpx.Response(200, json={"expires_in": 3600})

    with pytest.raises(AuthError):
        credentials.access_token(make_client(handler))


def test_secrets_are_absent_from_repr(credentials):
    rendered = repr(credentials)
    assert "refresh-token" not in rendered
    assert "client-secret" not in rendered
    assert "client-id" in rendered, "non-secret fields stay visible for debugging"
