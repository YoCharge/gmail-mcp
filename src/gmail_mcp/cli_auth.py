"""One-time consent flow.

Run once per mailbox. Opens a browser, captures the authorization code on a
loopback listener, exchanges it for a refresh token, verifies the token opens the
mailbox you asked for, and prints it. The token is never written to disk here —
you paste it into that mailbox's server entry.
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from .auth import TOKEN_URL, AuthError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# readonly covers search/read; compose additionally lets this server create, update,
# delete and send drafts (sending always takes a draft_id for a draft the caller
# already composed and showed the user — see send_draft). modify is what lets it
# add/remove labels and trash/untrash threads; it does NOT grant permanent delete,
# which Gmail only allows under the much broader https://mail.google.com/ scope —
# deliberately not requested here.
SCOPE = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/gmail.compose "
    "https://www.googleapis.com/auth/gmail.modify"
)
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# Served the moment the authorization code arrives, which is BEFORE the token
# exchange and the identity check. It must therefore not claim success: the run
# can still fail afterwards, and a page promising a token that never appears
# sends people back to a terminal they have been told to stop reading.
SUCCESS_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Authorization received</title>
<body style="font-family:system-ui;padding:3rem;max-width:32rem">
<h2>Authorization received.</h2>
<p>Return to your terminal to see whether it succeeded &mdash; there are still
two checks to run, and the refresh token is only printed if both pass.</p>
</body>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures ?code= from Google's redirect."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        _CallbackHandler.error = (params.get("error") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_PAGE)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; it would interleave with our output."""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def authorize(account: str, client_id: str, client_secret: str) -> str:
    """Run the consent flow and return a refresh token for `account`."""
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            # offline + consent together are what guarantee a refresh token comes back.
            # Without prompt=consent, a mailbox that has authorized before gets an
            # access token only, and the flow appears to succeed while returning nothing.
            "access_type": "offline",
            "prompt": "consent",
            "login_hint": account,
            "state": state,
        }
    )
    url = f"{AUTH_URL}?{query}"

    print(f"\nAuthorizing {account}")
    print("A browser window will open. Sign in as that mailbox and approve.")
    print(f"\nIf it does not open, visit:\n{url}\n")

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    webbrowser.open(url)
    server.handle_request()
    server.server_close()

    if _CallbackHandler.error:
        raise AuthError(f"Authorization was refused: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise AuthError("No authorization code was returned.")
    if _CallbackHandler.state != state:
        raise AuthError("State mismatch — discarding this response.")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": _CallbackHandler.code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        if response.status_code != 200:
            raise AuthError(f"Token exchange failed: {response.text}")

        payload = response.json()
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise AuthError(
                "Google returned no refresh_token. This usually means the mailbox had "
                "already granted access — revoke it at myaccount.google.com/permissions "
                "and run this again."
            )

        # Verify now rather than at first use: signing in as the wrong account is an
        # easy mistake when several mailboxes are open in one browser, and it would
        # otherwise surface much later as a confusing startup failure.
        profile = client.get(
            PROFILE_URL,
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        )

        # A failed check and a wrong account are different problems and must not
        # share an error message. Reporting "you signed in as the wrong account"
        # when the truth is "the API is switched off" sends people hunting the
        # wrong thing -- which is exactly what happened the first time this ran.
        if profile.status_code != 200:
            raise AuthError(_describe_profile_failure(profile))

        actual = profile.json().get("emailAddress", "")

        if actual.lower() != account.lower():
            raise AuthError(
                f"You asked to authorize {account} but signed in as {actual or 'an unnamed account'}. "
                f"Discarding this token — run again and pick the right account."
            )

    return refresh_token


def _describe_profile_failure(response: httpx.Response) -> str:
    """Explain why the post-consent identity check could not be made."""
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}

    reason = ""
    for detail in error.get("errors", []) or []:
        if detail.get("reason"):
            reason = detail["reason"]
            break

    message = error.get("message", f"HTTP {response.status_code}")
    text = (
        f"Consent succeeded, but the mailbox could not be verified: {message}. "
        f"Discarding this token."
    )

    if reason == "accessNotConfigured" or "has not been used" in message or "is disabled" in message:
        text += (
            "\n\nThe Gmail API is not enabled on this Google Cloud project. "
            "Enable it at APIs & Services -> Library -> Gmail API, wait a minute for "
            "it to propagate, then run this command again."
        )
    elif response.status_code == 403:
        text += (
            "\n\nA 403 here usually means the Gmail API is not enabled on the project, "
            "or the consent screen does not list the gmail.readonly, gmail.compose and "
            "gmail.modify scopes."
        )
    return text

def run() -> None:
    if len(sys.argv) != 2:
        print("usage: gmail-mcp-auth <mailbox@example.com>", file=sys.stderr)
        sys.exit(2)

    account = sys.argv[1].strip()

    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print(
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set.\n"
            "See docs/setup.md for creating the OAuth client.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        refresh_token = authorize(account, client_id, client_secret)
    except AuthError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 68)
    print(f"Refresh token for {account}:\n")
    print(refresh_token)
    print("\n" + "=" * 68)
    print("Paste this as GMAIL_REFRESH_TOKEN in that mailbox's server entry.")
    print("Treat it as a password: it opens this mailbox until revoked.")


if __name__ == "__main__":
    run()
