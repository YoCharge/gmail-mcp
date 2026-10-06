import json

import httpx
import pytest

from gmail_mcp.gmail import (
    GmailClient,
    GmailError,
    build_forward_mime_message,
    build_mime_message,
    build_reply_mime_message,
    decode_base64url,
    extract_body,
    forward_subject,
    header,
    list_attachments,
    other_recipients,
    reply_subject,
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


def test_build_mime_message_includes_html_body_as_an_alternative():
    raw = build_mime_message(
        to="a@example.com", body="hello", html_body="<b>hello</b>"
    )
    decoded = decode_base64url(raw)

    assert "Content-Type: multipart/alternative" in decoded
    assert "Content-Type: text/html" in decoded
    assert "hello" in decoded
    assert "<b>hello</b>" in decoded


def test_build_mime_message_omits_html_part_when_not_given():
    raw = build_mime_message(to="a@example.com", body="hello")
    decoded = decode_base64url(raw)

    assert "multipart/alternative" not in decoded
    assert "text/html" not in decoded


def test_build_mime_message_attaches_a_local_file(tmp_path):
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello world")

    raw = build_mime_message(to="a@example.com", body="hi", attachment_paths=[str(file_path)])
    decoded = decode_base64url(raw)

    assert "Content-Disposition: attachment" in decoded
    assert 'filename="notes.txt"' in decoded
    assert "aGVsbG8gd29ybGQ=" in decoded  # base64 of "hello world"


def test_build_mime_message_rejects_a_missing_attachment(tmp_path):
    missing = tmp_path / "missing.txt"
    with pytest.raises(ValueError, match="not found"):
        build_mime_message(to="a@example.com", attachment_paths=[str(missing)])


def test_build_mime_message_rejects_attachments_over_the_size_limit(tmp_path, monkeypatch):
    import gmail_mcp.gmail as gmail_module

    monkeypatch.setattr(gmail_module, "MAX_ATTACHMENTS_BYTES", 10)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 20)

    with pytest.raises(ValueError, match="limit"):
        build_mime_message(to="a@example.com", attachment_paths=[str(big)])


# -- reply / forward composition --------------------------------------------

ORIGINAL = {
    "id": "m1",
    "threadId": "t1",
    "payload": {
        "headers": [
            {"name": "Message-ID", "value": "<abc@example.com>"},
            {"name": "Subject", "value": "Hello"},
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "To", "value": "you@example.com, bob@example.com"},
            {"name": "Cc", "value": "carol@example.com"},
            {"name": "Date", "value": "Fri, 2 Oct 2026 10:00:00 +0000"},
        ],
        "mimeType": "text/plain",
        "body": {"data": b64("Hi there")},
    },
}


def test_reply_subject_adds_re_prefix_once():
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("Re: Hello") == "Re: Hello"
    assert reply_subject("") == "Re:"


def test_forward_subject_adds_fwd_prefix_once():
    assert forward_subject("Hello") == "Fwd: Hello"
    assert forward_subject("Fwd: Hello") == "Fwd: Hello"
    assert forward_subject("") == "Fwd:"


def test_build_reply_mime_message_threads_on_the_original():
    raw = build_reply_mime_message(ORIGINAL, "Thanks!", to="alice@example.com")
    decoded = decode_base64url(raw)

    assert "Subject: Re: Hello" in decoded
    assert "In-Reply-To: <abc@example.com>" in decoded
    assert "References: <abc@example.com>" in decoded
    assert "Thanks!" in decoded


def test_build_forward_mime_message_quotes_the_original():
    raw = build_forward_mime_message(ORIGINAL, "FYI", to="dave@example.com")
    decoded = decode_base64url(raw)

    assert "Subject: Fwd: Hello" in decoded
    assert "FYI" in decoded
    assert "From: Alice <alice@example.com>" in decoded
    assert "Hi there" in decoded


def test_other_recipients_excludes_the_account_and_merges_to_and_cc():
    assert other_recipients(ORIGINAL, "you@example.com") == "bob@example.com, carol@example.com"


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


# -- thread labeling and trash -----------------------------------------------


def test_label_thread_posts_add_label_ids(credentials):
    seen = {}

    def modify(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "t1"})

    client = GmailClient(credentials, make_client(routed({"/threads/t1/modify": modify})))
    client.label_thread("t1", ["STARRED"])

    assert seen["body"] == {"addLabelIds": ["STARRED"]}


def test_unlabel_thread_posts_remove_label_ids(credentials):
    seen = {}

    def modify(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "t1"})

    client = GmailClient(credentials, make_client(routed({"/threads/t1/modify": modify})))
    client.unlabel_thread("t1", ["STARRED"])

    assert seen["body"] == {"removeLabelIds": ["STARRED"]}


def test_trash_and_untrash_thread_hit_distinct_endpoints(credentials):
    client = GmailClient(
        credentials,
        make_client(
            routed(
                {
                    "/threads/t1/trash": httpx.Response(200, json={"id": "t1"}),
                    "/threads/t1/untrash": httpx.Response(200, json={"id": "t1"}),
                }
            )
        ),
    )

    assert client.trash_thread("t1")["id"] == "t1"
    assert client.untrash_thread("t1")["id"] == "t1"


def test_mark_thread_spam_flips_spam_on_and_inbox_off(credentials):
    seen = {}

    def modify(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "t1"})

    client = GmailClient(credentials, make_client(routed({"/threads/t1/modify": modify})))
    client.mark_thread_spam("t1")

    assert seen["body"] == {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]}


def test_unmark_thread_spam_flips_inbox_on_and_spam_off(credentials):
    seen = {}

    def modify(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "t1"})

    client = GmailClient(credentials, make_client(routed({"/threads/t1/modify": modify})))
    client.unmark_thread_spam("t1")

    assert seen["body"] == {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]}


# -- label definitions --------------------------------------------------------


def test_create_label_posts_name_and_visibility_defaults(credentials):
    seen = {}

    def create(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "Label_1", "name": "Projects"})

    client = GmailClient(credentials, make_client(routed({"/labels": create})))
    result = client.create_label("Projects")

    assert result["id"] == "Label_1"
    assert seen["body"] == {
        "name": "Projects",
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }


def test_update_label_sends_a_partial_patch(credentials):
    seen = {}

    def update(request):
        assert request.method == "PATCH"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "Label_1", "name": "Archive"})

    client = GmailClient(credentials, make_client(routed({"/labels/Label_1": update})))
    result = client.update_label("Label_1", name="Archive")

    assert result["name"] == "Archive"
    assert seen["body"] == {"name": "Archive"}


def test_delete_label_handles_the_empty_204_body(credentials):
    client = GmailClient(
        credentials, make_client(routed({"/labels/Label_1": httpx.Response(204)}))
    )
    assert client.delete_label("Label_1") is None


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
