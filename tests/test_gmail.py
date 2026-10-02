import json

import httpx
import pytest

from gmail_mcp.gmail import (
    GmailClient,
    GmailError,
    build_mime_message,
    decode_base64url,
    extract_body,
    header,
    list_attachments,
    summarise_message,
)
from conftest import b64, make_client


def routed(routes):
    """Dispatch by URL path, always answering the token endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        for fragment, response in routes.items():
            if fragment in str(request.url):
                return response(request) if callable(response) else response
        return httpx.Response(404, json={"error": {"message": "no route"}})

    return handler


def test_get_profile_returns_the_mailbox_identity(credentials):
    client = GmailClient(
        credentials,
        make_client(routed({"/profile": httpx.Response(200, json={"emailAddress": "a@example.com"})})),
    )
    assert client.get_profile()["emailAddress"] == "a@example.com"


def test_a_401_triggers_one_refresh_and_retry(credentials):
    attempts = {"count": 0}

    def profile(request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"emailAddress": "a@example.com"})

    client = GmailClient(credentials, make_client(routed({"/profile": profile})))

    assert client.get_profile()["emailAddress"] == "a@example.com"
    assert attempts["count"] == 2, "should retry exactly once after refreshing"


def test_a_persistent_401_gives_up_rather_than_looping(credentials):
    attempts = {"count": 0}

    def profile(request):
        attempts["count"] += 1
        return httpx.Response(401, json={"error": {"message": "revoked"}})

    client = GmailClient(credentials, make_client(routed({"/profile": profile})))

    with pytest.raises(GmailError):
        client.get_profile()
    assert attempts["count"] == 2


def test_api_errors_surface_the_message(credentials):
    client = GmailClient(
        credentials,
        make_client(routed({"/threads/": httpx.Response(404, json={"error": {"message": "Not Found"}})})),
    )
    with pytest.raises(GmailError, match="Not Found"):
        client.get_thread("missing")


def test_max_results_is_clamped_to_the_api_limit(credentials):
    seen = {}

    def threads(request):
        seen["max"] = request.url.params.get("maxResults")
        return httpx.Response(200, json={"threads": []})

    client = GmailClient(credentials, make_client(routed({"/threads": threads})))
    client.list_threads("is:unread", max_results=99999)

    assert seen["max"] == "500"


# -- payload parsing -------------------------------------------------------


def test_decodes_base64url_without_padding():
    assert decode_base64url(b64("hello?>")) == "hello?>"


def test_header_lookup_ignores_case():
    message = {"payload": {"headers": [{"name": "Subject", "value": "Alert"}]}}
    assert header(message, "subject") == "Alert"
    assert header(message, "Missing") == ""


def test_plain_text_is_preferred_over_html():
    message = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("plain version")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>html version</p>")}},
            ],
        }
    }
    assert extract_body(message) == "plain version"


def test_html_is_used_when_there_is_no_plain_part():
    message = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": b64("<p>only html</p>")},
        }
    }
    assert "only html" in extract_body(message)


def test_bodies_nested_several_levels_deep_are_found():
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [{"mimeType": "text/plain", "body": {"data": b64("buried")}}],
                }
            ],
        }
    }
    assert extract_body(message) == "buried"


def test_attachments_are_listed_with_their_ids():
    message = {
        "payload": {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("see attached")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att-1", "size": 2048},
                },
            ]
        }
    }
    attachments = list_attachments(message)

    assert len(attachments) == 1
    assert attachments[0]["attachment_id"] == "att-1"
    assert attachments[0]["filename"] == "invoice.pdf"


def test_build_mime_message_includes_the_given_fields():
    raw = build_mime_message(to="a@example.com", subject="Hi", body="hello", cc="b@example.com")
    decoded = decode_base64url(raw)

    assert "To: a@example.com" in decoded
    assert "Cc: b@example.com" in decoded
    assert "Subject: Hi" in decoded
    assert "hello" in decoded
    assert "From:" not in decoded


# -- drafts -----------------------------------------------------------------


def test_list_drafts_returns_the_drafts_list(credentials):
    client = GmailClient(
        credentials,
        make_client(routed({"/drafts": httpx.Response(200, json={"drafts": [{"id": "d1"}]})})),
    )
    assert client.list_drafts() == [{"id": "d1"}]


def test_create_draft_posts_the_raw_message(credentials):
    seen = {}

    def create(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "d1"})

    client = GmailClient(credentials, make_client(routed({"/drafts": create})))
    result = client.create_draft("raw-bytes", thread_id="t1")

    assert result["id"] == "d1"
    assert seen["body"] == {"message": {"raw": "raw-bytes", "threadId": "t1"}}


def test_delete_draft_handles_the_empty_204_body(credentials):
    client = GmailClient(
        credentials, make_client(routed({"/drafts/d1": httpx.Response(204)}))
    )
    assert client.delete_draft("d1") is None


def test_send_draft_posts_only_the_draft_id(credentials):
    seen = {}

    def send(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "m1"})

    client = GmailClient(credentials, make_client(routed({"/drafts/send": send})))
    result = client.send_draft("d1")

    assert result["id"] == "m1"
    assert seen["body"] == {"id": "d1"}


def test_summarise_can_omit_the_body():
    message = {
        "id": "m1",
        "payload": {
            "headers": [{"name": "From", "value": "a@example.com"}],
            "mimeType": "text/plain",
            "body": {"data": b64("secret")},
        },
    }
    assert "body" not in summarise_message(message, include_body=False)
    assert summarise_message(message)["body"] == "secret"
