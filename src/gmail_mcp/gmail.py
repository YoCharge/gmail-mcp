"""A thin Gmail REST client.

Read surface, draft management (including replying and forwarding, both compose-only
via create_draft), and labeling/trashing/spam for both threads and individual
messages, plus label definitions (create/update/delete). Still only one way to
actually send anything — send_draft, by draft id — so replying and forwarding pass
through the same confirm-before-send gate as every other draft. Trash and spam are
reversible pairs (Gmail keeps trashed mail ~30 days; untrash/unmark undo them
exactly); there is deliberately no permanent-delete-a-message call, which Gmail only
allows under the much broader https://mail.google.com/ scope. Deleting a label
definition (delete_label) is the one irreversible call here — every message loses
that label, and recreating a same-named label does not relink them.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Iterator

import httpx

API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

DEFAULT_TIMEOUT = 30.0


class GmailError(RuntimeError):
    """The Gmail API returned an error."""


class GmailClient:
    def __init__(self, credentials, client: httpx.Client | None = None) -> None:
        self._credentials = credentials
        self._http = client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET with a single retry after a forced token refresh.

        A 401 here is nearly always a token that expired mid-session rather than a
        genuine authorization failure, so one refresh-and-retry is worth it. Retrying
        more than once would just hammer the endpoint with the same bad credential.
        """
        url = f"{API_ROOT}{path}"

        token = self._credentials.access_token(self._http)
        response = self._http.get(url, params=params, headers=_bearer(token))

        if response.status_code == 401:
            token = self._credentials.refresh(self._http)
            response = self._http.get(url, params=params, headers=_bearer(token))

        if response.status_code >= 400:
            raise GmailError(_describe_failure(response))

        return response.json()

    def _write(self, method: str, path: str, json_body: dict | None = None) -> dict:
        """POST/PUT/DELETE with the same one-retry-after-refresh behaviour as _get."""
        url = f"{API_ROOT}{path}"

        token = self._credentials.access_token(self._http)
        response = self._http.request(method, url, json=json_body, headers=_bearer(token))

        if response.status_code == 401:
            token = self._credentials.refresh(self._http)
            response = self._http.request(method, url, json=json_body, headers=_bearer(token))

        if response.status_code >= 400:
            raise GmailError(_describe_failure(response))

        # DELETE returns 204 with an empty body; nothing else here does.
        return response.json() if response.content else {}

    # -- read surface ------------------------------------------------------

    def get_profile(self) -> dict:
        """Identity of the mailbox this token actually opens. Used for the startup assertion."""
        return self._get("/profile")

    def list_threads(self, query: str, max_results: int = 20) -> list[dict]:
        payload = self._get(
            "/threads",
            params={"q": query, "maxResults": max(1, min(max_results, 500))},
        )
        return payload.get("threads", [])

    def get_thread(self, thread_id: str) -> dict:
        return self._get(f"/threads/{thread_id}", params={"format": "full"})

    def get_message(self, message_id: str) -> dict:
        return self._get(f"/messages/{message_id}", params={"format": "full"})

    def list_labels(self) -> list[dict]:
        return self._get("/labels").get("labels", [])

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        payload = self._get(f"/messages/{message_id}/attachments/{attachment_id}")
        return decode_base64url_bytes(payload.get("data", ""))

    # -- draft management ----------------------------------------------------
    #
    # Composing and sending are two different calls on purpose. create_draft only
    # ever writes to the Drafts folder; send_draft is the one call in this entire
    # client that can put a message in someone's inbox, and it takes a draft_id
    # rather than a to/subject/body, so the caller must already have the exact
    # content on screen (from create_draft's or get_draft's response) before they
    # can send it — there is no path that composes and sends in one step.

    def list_drafts(self, max_results: int = 20) -> list[dict]:
        payload = self._get("/drafts", params={"maxResults": max(1, min(max_results, 500))})
        return payload.get("drafts", [])

    def get_draft(self, draft_id: str) -> dict:
        return self._get(f"/drafts/{draft_id}", params={"format": "full"})

    def create_draft(self, raw_message: str, thread_id: str | None = None) -> dict:
        message: dict[str, Any] = {"raw": raw_message}
        if thread_id:
            message["threadId"] = thread_id
        return self._write("POST", "/drafts", {"message": message})

    def update_draft(self, draft_id: str, raw_message: str, thread_id: str | None = None) -> dict:
        message: dict[str, Any] = {"raw": raw_message}
        if thread_id:
            message["threadId"] = thread_id
        return self._write("PUT", f"/drafts/{draft_id}", {"message": message})

    def delete_draft(self, draft_id: str) -> None:
        self._write("DELETE", f"/drafts/{draft_id}")

    def send_draft(self, draft_id: str) -> dict:
        """Send an existing draft. Irreversible — the message leaves the mailbox."""
        return self._write("POST", "/drafts/send", {"id": draft_id})

    # -- thread labeling and trash --------------------------------------------
    #
    # trash/untrash are a reversible pair: Gmail keeps trashed mail ~30 days, and
    # untrash_thread undoes trash_thread exactly. There is no permanent-delete call;
    # Gmail only exposes that under the much broader https://mail.google.com/ scope.

    def label_thread(self, thread_id: str, label_ids: list[str]) -> dict:
        return self._write("POST", f"/threads/{thread_id}/modify", {"addLabelIds": label_ids})

    def unlabel_thread(self, thread_id: str, label_ids: list[str]) -> dict:
        return self._write("POST", f"/threads/{thread_id}/modify", {"removeLabelIds": label_ids})

    def trash_thread(self, thread_id: str) -> dict:
        return self._write("POST", f"/threads/{thread_id}/trash")

    def mark_thread_spam(self, thread_id: str) -> dict:
        """Flip SPAM on and INBOX off. Undo with unmark_thread_spam."""
        return self._write(
            "POST", f"/threads/{thread_id}/modify",
            {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]},
        )

    def unmark_thread_spam(self, thread_id: str) -> dict:
        """Flip INBOX on and SPAM off — undoes mark_thread_spam exactly."""
        return self._write(
            "POST", f"/threads/{thread_id}/modify",
            {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]},
        )

    def untrash_thread(self, thread_id: str) -> dict:
        return self._write("POST", f"/threads/{thread_id}/untrash")

    # -- message labeling and trash -------------------------------------------
    #
    # Same shape as the thread-level calls above, one message at a time: trashing
    # or spam-marking a single message does not touch the rest of its thread.

    def label_message(self, message_id: str, label_ids: list[str]) -> dict:
        return self._write("POST", f"/messages/{message_id}/modify", {"addLabelIds": label_ids})

    def unlabel_message(self, message_id: str, label_ids: list[str]) -> dict:
        return self._write(
            "POST", f"/messages/{message_id}/modify", {"removeLabelIds": label_ids}
        )

    def update_message_labels(
        self,
        message_id: str,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> dict:
        """Add and remove labels in the same call, unlike label_message/unlabel_message."""
        body: dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        return self._write("POST", f"/messages/{message_id}/modify", body)

    def trash_message(self, message_id: str) -> dict:
        return self._write("POST", f"/messages/{message_id}/trash")

    def untrash_message(self, message_id: str) -> dict:
        return self._write("POST", f"/messages/{message_id}/untrash")

    def mark_message_spam(self, message_id: str) -> dict:
        """Flip SPAM on and INBOX off. Undo with unmark_message_spam."""
        return self._write(
            "POST", f"/messages/{message_id}/modify",
            {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]},
        )

    def unmark_message_spam(self, message_id: str) -> dict:
        """Flip INBOX on and SPAM off — undoes mark_message_spam exactly."""
        return self._write(
            "POST", f"/messages/{message_id}/modify",
            {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]},
        )

    # -- label definitions -----------------------------------------------------
    #
    # Creating, renaming or deleting a label definition itself, as distinct from
    # applying one to a message/thread. Deleting one is NOT reversible: every
    # message loses it, and recreating a same-named label does not relink them.

    def create_label(
        self,
        name: str,
        label_list_visibility: str = "labelShow",
        message_list_visibility: str = "show",
    ) -> dict:
        return self._write(
            "POST", "/labels",
            {
                "name": name,
                "labelListVisibility": label_list_visibility,
                "messageListVisibility": message_list_visibility,
            },
        )

    def update_label(
        self,
        label_id: str,
        name: str | None = None,
        label_list_visibility: str | None = None,
        message_list_visibility: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if label_list_visibility is not None:
            body["labelListVisibility"] = label_list_visibility
        if message_list_visibility is not None:
            body["messageListVisibility"] = message_list_visibility
        return self._write("PATCH", f"/labels/{label_id}", body)

    def delete_label(self, label_id: str) -> None:
        self._write("DELETE", f"/labels/{label_id}")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _describe_failure(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"

    error = body.get("error", {})
    message = error.get("message", f"HTTP {response.status_code}")
    return f"{response.status_code}: {message}"


# -- payload helpers -------------------------------------------------------
#
# Gmail nests message bodies in a parts tree and base64url-encodes every leaf.
# These are pure functions so they can be tested against captured payloads without
# any network involved.


def build_mime_message(
    to: str,
    subject: str = "",
    body: str = "",
    cc: str | None = None,
    bcc: str | None = None,
) -> str:
    """Build an RFC 822 message and base64url-encode it the way drafts.create expects.

    From is left unset: Gmail fills it in as the authenticated mailbox, which is the
    only address this token could plausibly send as.
    """
    message = EmailMessage()
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def reply_subject(subject: str) -> str:
    if not subject:
        return "Re:"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def forward_subject(subject: str) -> str:
    if not subject:
        return "Fwd:"
    return subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}"


def build_reply_mime_message(
    original: dict,
    body: str,
    to: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> str:
    """Build a reply threaded to `original` via In-Reply-To/References.

    The caller is also expected to pass the original's threadId to create_draft:
    that's what groups the reply in Gmail's own UI. These headers are what other
    mail clients thread on, so both are set.
    """
    message_id = header(original, "Message-ID") or header(original, "Message-Id")
    references = " ".join(part for part in (header(original, "References"), message_id) if part)

    message = EmailMessage()
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = reply_subject(header(original, "Subject"))
    if message_id:
        message["In-Reply-To"] = message_id
    if references:
        message["References"] = references
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def build_forward_mime_message(
    original: dict,
    body: str,
    to: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> str:
    """Build a forward: a new message quoting `original`'s headers and body.

    Attachments on the original are not carried over — forwarding one would require
    downloading and re-attaching its bytes, which no tool here does yet.
    """
    quoted = (
        "---------- Forwarded message ----------\n"
        f"From: {header(original, 'From')}\n"
        f"Date: {header(original, 'Date')}\n"
        f"Subject: {header(original, 'Subject')}\n"
        f"To: {header(original, 'To')}\n\n"
        f"{extract_body(original)}"
    )
    full_body = f"{body}\n\n{quoted}" if body else quoted

    message = EmailMessage()
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = forward_subject(header(original, "Subject"))
    message.set_content(full_body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def other_recipients(original: dict, account: str) -> str:
    """Everyone on `original`'s To/Cc except `account`, comma-joined — for reply-all."""
    addresses = []
    for field in (header(original, "To"), header(original, "Cc")):
        for part in field.split(","):
            part = part.strip()
            if part and account.lower() not in part.lower():
                addresses.append(part)
    return ", ".join(addresses)


def decode_base64url(data: str) -> str:
    return decode_base64url_bytes(data).decode("utf-8", errors="replace")


def decode_base64url_bytes(data: str) -> bytes:
    if not data:
        return b""
    # Gmail omits padding; base64 requires the length to be a multiple of four.
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def header(message: dict, name: str) -> str:
    """Case-insensitive header lookup. Returns '' when absent."""
    wanted = name.lower()
    for entry in message.get("payload", {}).get("headers", []):
        if entry.get("name", "").lower() == wanted:
            return entry.get("value", "")
    return ""


def walk_parts(payload: dict) -> Iterator[dict]:
    """Depth-first walk of the MIME tree, including the root."""
    if not payload:
        return
    yield payload
    for part in payload.get("parts", []) or []:
        yield from walk_parts(part)


def extract_body(message: dict) -> str:
    """Best-effort plain text for a message.

    Prefers text/plain. Falls back to text/html only when there is no plain part,
    because multipart/alternative messages carry both and the HTML copy is far
    noisier to read.
    """
    payload = message.get("payload", {})

    plain: list[str] = []
    html: list[str] = []

    for part in walk_parts(payload):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(decode_base64url(data))
        elif mime == "text/html":
            html.append(decode_base64url(data))

    if plain:
        return "\n".join(plain).strip()
    if html:
        return "\n".join(html).strip()
    return ""


def list_attachments(message: dict) -> list[dict]:
    """Attachment parts with the ids download_attachment needs."""
    found = []
    for part in walk_parts(message.get("payload", {})):
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        if not attachment_id:
            continue
        found.append(
            {
                "attachment_id": attachment_id,
                "filename": part.get("filename") or "unnamed",
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "size": body.get("size", 0),
            }
        )
    return found


def summarise_message(message: dict, include_body: bool = True) -> dict:
    """Flatten a Gmail message into something readable in a tool result."""
    summary = {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "date": header(message, "Date"),
        "from": header(message, "From"),
        "to": header(message, "To"),
        "subject": header(message, "Subject"),
        "labels": message.get("labelIds", []),
        "snippet": message.get("snippet", ""),
    }

    attachments = list_attachments(message)
    if attachments:
        summary["attachments"] = attachments
    if include_body:
        summary["body"] = extract_body(message)
    return summary
