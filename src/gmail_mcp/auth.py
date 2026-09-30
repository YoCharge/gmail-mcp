"""OAuth token lifecycle.

A refresh token is long-lived and lives in the environment. Access tokens last about
an hour and never touch disk. This module is the only place that talks to Google's
token endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh slightly before real expiry so a long request cannot straddle the boundary.
EXPIRY_MARGIN_SECONDS = 60


class AuthError(RuntimeError):
    """Google refused to issue an access token."""


@dataclass
class Credentials:
    """One mailbox's OAuth credentials.

    Secrets are marked repr=False so an accidental log line or traceback cannot
    print the refresh token.
    """

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    _access_token: str | None = field(default=None, repr=False)
    _expires_at: float = field(default=0.0, repr=False)

    def access_token(self, client: httpx.Client) -> str:
        """Return a usable access token, refreshing only when the cached one is stale."""
        if self._access_token and time.time() < self._expires_at - EXPIRY_MARGIN_SECONDS:
            return self._access_token
        return self.refresh(client)

    def refresh(self, client: httpx.Client) -> str:
        """Force a token exchange, discarding any cached access token."""
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if response.status_code != 200:
            raise AuthError(_describe_failure(response))

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthError("Google returned 200 but no access_token.")

        self._access_token = token
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return token


def _describe_failure(response: httpx.Response) -> str:
    """Build an error message from Google's response without echoing the request.

    Google's token errors are terse; the most common by far is invalid_grant, which
    almost always means the refresh token was revoked or the consent screen is still
    in Testing mode (where tokens expire after seven days).
    """
    try:
        body = response.json()
    except ValueError:
        body = {}

    code = body.get("error", f"HTTP {response.status_code}")
    detail = body.get("error_description", "")

    message = f"Token refresh failed: {code}"
    if detail:
        message += f" — {detail}"
    if code == "invalid_grant":
        message += (
            "\nThe refresh token is no longer valid. Either it was revoked, or the "
            "OAuth consent screen is still in Testing mode (tokens expire after 7 days). "
            "Publish the consent screen as Internal, then re-run gmail-mcp-auth."
        )
    return message
