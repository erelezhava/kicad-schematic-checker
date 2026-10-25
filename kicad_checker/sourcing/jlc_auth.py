"""JLCPCB JOP HMAC-SHA256 request authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass
from time import time
from urllib.parse import urlsplit


NONCE_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
NONCE_LENGTH = 32


def canonical_uri(url_or_path: str) -> str:
    """Return the raw URL path plus raw query string used by the SDK signer."""

    parsed = urlsplit(url_or_path)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def build_string_to_sign(
    method: str,
    url_or_path: str,
    timestamp: int | str,
    nonce: str,
    body: str,
) -> str:
    """Build the five-line JOP signing payload, including its final newline."""

    return "\n".join(
        (method.upper(), canonical_uri(url_or_path), str(timestamp), nonce, body, "")
    )


def generate_nonce(length: int = NONCE_LENGTH) -> str:
    """Generate a cryptographically random SDK-compatible nonce."""

    if length <= 0:
        raise ValueError("Nonce length must be positive")
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class AuthorizationResult:
    """Generated header and signing inputs, useful for request preparation and tests."""

    header: str
    string_to_sign: str
    timestamp: int
    nonce: str


@dataclass(frozen=True)
class JLCPCBAuthenticator:
    """Create the exact ``Authorization`` header expected by JLCPCB's JOP API."""

    app_id: str
    access_key: str
    secret_key: str

    def authorize(
        self,
        method: str,
        url_or_path: str,
        body: str,
        *,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> AuthorizationResult:
        request_timestamp = int(time()) if timestamp is None else int(timestamp)
        request_nonce = generate_nonce() if nonce is None else nonce
        if len(request_nonce) != NONCE_LENGTH:
            raise ValueError(f"Nonce must be exactly {NONCE_LENGTH} characters")

        string_to_sign = build_string_to_sign(
            method, url_or_path, request_timestamp, request_nonce, body
        )
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        header = (
            f'JOP-HmacSHA256 appid="{self.app_id}",accesskey="{self.access_key}",'
            f'timestamp="{request_timestamp}",nonce="{request_nonce}",signature="{signature}"'
        )
        return AuthorizationResult(header, string_to_sign, request_timestamp, request_nonce)
