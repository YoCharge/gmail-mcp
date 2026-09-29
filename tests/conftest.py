import base64

import httpx
import pytest

from gmail_mcp.auth import Credentials


def b64(text: str) -> str:
    """Base64url without padding, the way Gmail encodes bodies."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
    )
