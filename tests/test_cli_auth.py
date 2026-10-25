"""Regression tests for the post-consent identity check.

The first real run of this flow reported "signed in as an unknown account" when
the actual problem was that the Gmail API was switched off on the Cloud project.
A failed check and a wrong account must never share a message.
"""

import httpx

from gmail_mcp.cli_auth import _describe_profile_failure


def response(status, payload):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://example.com"))


def test_a_disabled_api_is_named_explicitly():
    failure = _describe_profile_failure(
        response(
            403,
            {
                "error": {
                    "message": "Gmail API has not been used in project 1234 before or it is disabled.",
                    "errors": [{"reason": "accessNotConfigured"}],
                }
            },
        )
    )

    assert "Gmail API is not enabled" in failure
    assert "APIs & Services" in failure
    assert "signed in as" not in failure, "this is not an account mismatch"


def test_a_bare_403_still_points_at_the_likely_cause():
    failure = _describe_profile_failure(response(403, {"error": {"message": "Forbidden"}}))

    assert "not enabled" in failure or "scope" in failure


def test_an_unexpected_status_is_reported_rather_than_guessed():
    failure = _describe_profile_failure(response(500, {"error": {"message": "Backend error"}}))

    assert "Backend error" in failure
    assert "Discarding this token" in failure


def test_a_non_json_body_does_not_crash_the_explanation():
    raw = httpx.Response(502, text="<html>bad gateway</html>",
                         request=httpx.Request("GET", "https://example.com"))
    assert "502" in _describe_profile_failure(raw)


# --- least privilege: the consent screen asks only for what the mailbox's tier uses ---

import pytest

from gmail_mcp.cli_auth import scopes_for
from gmail_mcp.server import ConfigError

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"


def test_a_read_mailbox_asks_for_read_only():
    assert scopes_for("read") == READONLY


def test_draft_and_send_mailboxes_ask_for_compose_and_modify():
    for tier in ("read,draft", "read,draft,send"):
        assert set(scopes_for(tier).split()) == {READONLY, COMPOSE, MODIFY}


def test_no_or_unknown_tier_is_refused_rather_than_guessed():
    for bad in ("", "send", "write"):
        with pytest.raises(ConfigError):
            scopes_for(bad)
