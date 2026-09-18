"""Single-household auth: one dashboard password -> signed session cookie; bearer tokens for scripts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, WebSocket

from .config import Settings, csv_list

COOKIE_NAME = "hs_session"
MAX_FAILURES = 5
FAILURE_WINDOW = 300  # seconds


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _session_key(settings: Settings) -> bytes:
    """SESSION_SECRET if set; otherwise one generated once and kept in the data volume."""
    if settings.session_secret:
        return settings.session_secret.encode()
    path = settings.data_dir / "session.key"
    try:
        key = path.read_bytes().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        path.chmod(0o600)
    except OSError:
        pass  # unwritable data dir: sessions just won't survive a restart
    return key


class Authenticator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._password = settings.dashboard_password
        self.open = settings.auth_open
        self._key = _session_key(settings)
        self._tokens = csv_list(settings.api_tokens)
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    # --- session tokens -----------------------------------------------------------------

    def issue(self) -> str:
        payload = {"exp": int(time.time()) + self.settings.session_hours * 3600, "n": secrets.token_hex(8)}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        body, _, signature = token.partition(".")
        if not hmac.compare_digest(signature, self._sign(body)):
            return False
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and int(payload.get("exp", 0)) > time.time()

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    # --- login --------------------------------------------------------------------------

    def check_password(self, password: str, client: str) -> bool:
        attempts = self._failures[client]
        now = time.monotonic()
        while attempts and now - attempts[0] > FAILURE_WINDOW:
            attempts.popleft()
        if len(attempts) >= MAX_FAILURES:
            raise HTTPException(429, "Too many attempts; wait a few minutes")
        if self._password and hmac.compare_digest(password.encode(), self._password.encode()):
            attempts.clear()
            return True
        attempts.append(now)
        return False

    # --- request checks -----------------------------------------------------------------

    def _bearer_ok(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        return any(hmac.compare_digest(token, allowed) for allowed in self._tokens)

    def is_authenticated(self, request: Request | WebSocket) -> bool:
        if self.open:
            return True
        return self.verify(request.cookies.get(COOKIE_NAME)) or self._bearer_ok(request.headers.get("authorization"))


def client_id(request: Request | WebSocket) -> str:
    return request.client.host if request.client else "unknown"


def require_user(request: Request) -> None:
    auth: Authenticator = request.app.state.auth
    if not auth.is_authenticated(request):
        raise HTTPException(401, "Not signed in")
