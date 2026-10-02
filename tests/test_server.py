import json

import httpx
import pytest

from gmail_mcp.gmail import GmailClient, decode_base64url
from gmail_mcp.server import (
    TOOL_NAMES,
    ConfigError,
    _dispatch,
    assert_account,
    build_server,
    load_config,
    resolve_dest_dir,
    safe_filename,
    tool_descriptions,
)
from conftest import b64, make_client

FULL_ENV = {
    "GMAIL_ACCOUNT": "you@example.com",
    "GMAIL_CLIENT_ID": "client-id",
    "GMAIL_CLIENT_SECRET": "client-secret",
    "GMAIL_REFRESH_TOKEN": "refresh-token",
}


# -- configuration ---------------------------------------------------------


def test_refuses_to_start_without_an_account():
    env = {k: v for k, v in FULL_ENV.items() if k != "GMAIL_ACCOUNT"}
    with pytest.raises(ConfigError, match="GMAIL_ACCOUNT"):
        load_config(env)


def test_refuses_to_start_without_credentials():
    env = {k: v for k, v in FULL_ENV.items() if k != "GMAIL_REFRESH_TOKEN"}
    with pytest.raises(ConfigError, match="GMAIL_REFRESH_TOKEN"):
        load_config(env)


def test_download_dir_is_optional():
    account, credentials, download_dir = load_config(FULL_ENV)
    assert account == "you@example.com"
    assert credentials.refresh_token == "refresh-token"
    assert download_dir is None


# -- the startup identity assertion ---------------------------------------
#
# This is the guarantee that makes one-process-per-mailbox real. If these tests
# disappear, so does the guarantee.


def profile_client(credentials, address):
    def handler(request):
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"emailAddress": address})

    return GmailClient(credentials, make_client(handler))


def test_a_matching_token_is_accepted(credentials):
    client = profile_client(credentials, "you@example.com")
    assert assert_account(client, "you@example.com") == "you@example.com"


def test_address_comparison_ignores_case(credentials):
    client = profile_client(credentials, "You@Example.com")
    assert assert_account(client, "you@example.com")


def test_a_token_for_another_mailbox_is_refused(credentials):
    """The whole point: a misplaced token must fail loudly, not serve the wrong inbox."""
    client = profile_client(credentials, "someone-else@example.com")

    with pytest.raises(ConfigError) as exc:
        assert_account(client, "you@example.com")

    message = str(exc.value)
    assert "someone-else@example.com" in message
    assert "you@example.com" in message


# -- tool surface ----------------------------------------------------------


def test_every_tool_names_its_mailbox():
    """With several servers registered, the description is what disambiguates them."""
    descriptions = tool_descriptions("you@example.com")

    assert set(descriptions) == set(TOOL_NAMES)
    for name, text in descriptions.items():
        assert "you@example.com" in text, f"{name} does not name its mailbox"


def test_the_surface_has_no_send_capability():
    """Drafts are allowed; nothing here can put a message in anyone's inbox."""
    names = set(TOOL_NAMES)

    assert names == {
        "search_threads",
        "get_thread",
        "get_message",
        "list_labels",
        "download_attachment",
        "list_drafts",
        "get_draft",
        "create_draft",
        "update_draft",
        "delete_draft",
    }
    for forbidden in ("send", "reply", "forward", "trash", "label_message"):
        assert forbidden not in names


async def test_the_server_registers_exactly_those_tools(credentials):
    """Guards the wiring itself: a tool defined but never registered is invisible."""
    client = GmailClient(credentials, make_client(lambda r: httpx.Response(200, json={})))
    server = build_server(client, "you@example.com", None)

    registered = await server.list_tools()

    assert {tool.name for tool in registered} == set(TOOL_NAMES)
    for tool in registered:
        assert "you@example.com" in tool.description


# -- attachment handling ---------------------------------------------------


def test_a_destination_is_required(tmp_path):
    with pytest.raises(ValueError, match="No destination directory"):
        resolve_dest_dir(None, None)


def test_the_env_fallback_is_used_when_no_dest_dir_is_given(tmp_path):
    assert resolve_dest_dir(None, str(tmp_path)) == tmp_path


def test_an_explicit_dest_dir_wins_over_the_fallback(tmp_path):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert resolve_dest_dir(str(explicit), str(tmp_path)) == explicit


def test_a_missing_directory_is_reported_rather_than_created(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        resolve_dest_dir(str(tmp_path / "nope"), None)


def test_path_traversal_in_a_filename_is_stripped():
    """The sender chooses this string, so it is never trusted as a path."""
    assert safe_filename("../../.ssh/authorized_keys") == "authorized_keys"
    assert safe_filename("/etc/passwd") == "passwd"
    assert safe_filename("") == "attachment.bin"
    assert safe_filename("invoice.pdf") == "invoice.pdf"


def test_download_writes_the_attachment_and_reports_the_path(credentials, tmp_path):
    message = {
        "id": "m1",
        "payload": {
            "headers": [{"name": "Subject", "value": "Invoice"}],
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att-1", "size": 5},
                }
            ],
        },
    }

    def handler(request):
        url = str(request.url)
        if "oauth2.googleapis.com" in url:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/attachments/att-1" in url:
            return httpx.Response(200, json={"data": b64("PDF!!")})
        return httpx.Response(200, json=message)

    client = GmailClient(credentials, make_client(handler))

    result = json.loads(
        _dispatch(client, "you@example.com", None, "download_attachment",
                  {"message_id": "m1", "attachment_id": "att-1", "dest_dir": str(tmp_path)})
    )

    written = tmp_path / "invoice.pdf"
    assert written.read_text() == "PDF!!"
    assert result["path"] == str(written)
    assert result["bytes"] == 5


# -- dispatch --------------------------------------------------------------


def test_every_result_reports_which_mailbox_it_came_from(credentials):
    def handler(request):
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"threads": [{"id": "t1", "snippet": "hi"}]})

    client = GmailClient(credentials, make_client(handler))
    result = json.loads(
        _dispatch(client, "you@example.com", None, "search_threads", {"query": "is:unread"})
    )

    assert result["account"] == "you@example.com"
    assert result["count"] == 1


def test_an_unknown_tool_is_rejected(credentials):
    client = GmailClient(credentials, make_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="Unknown tool"):
        _dispatch(client, "you@example.com", None, "send_message", {})


# -- drafts ------------------------------------------------------------------


def test_list_drafts_reports_the_count(credentials):
    def handler(request):
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"drafts": [{"id": "d1"}, {"id": "d2"}]})

    client = GmailClient(credentials, make_client(handler))
    result = json.loads(_dispatch(client, "you@example.com", None, "list_drafts", {}))

    assert result["count"] == 2
    assert [d["id"] for d in result["drafts"]] == ["d1", "d2"]


def test_create_draft_sends_a_base64url_encoded_message(credentials):
    seen = {}

    def handler(request):
        url = str(request.url)
        if "oauth2.googleapis.com" in url:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.method == "POST" and "/drafts" in url:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "d1"})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    client = GmailClient(credentials, make_client(handler))
    result = json.loads(
        _dispatch(
            client,
            "you@example.com",
            None,
            "create_draft",
            {"to": "a@example.com", "subject": "Hi", "body": "Hello there"},
        )
    )

    assert result["draft_id"] == "d1"
    raw = decode_base64url(seen["body"]["message"]["raw"])
    assert "To: a@example.com" in raw
    assert "Subject: Hi" in raw
    assert "Hello there" in raw


def test_delete_draft_reports_what_was_deleted(credentials):
    def handler(request):
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(204)

    client = GmailClient(credentials, make_client(handler))
    result = json.loads(
        _dispatch(client, "you@example.com", None, "delete_draft", {"draft_id": "d1"})
    )

    assert result["deleted"] == "d1"
