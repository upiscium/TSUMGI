"""HTTP Basic authentication for the operator console.

The password is never stored: the Control config holds a scrypt hash made by
``python -m astrumweaver.control.admin_auth``. Verification is constant time,
runs the same work for an unknown user name, and repeated failures from one
client are throttled (the throttle also applies to the right password, so a
guessing run cannot tell when it hit).
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import logging
import os
import sys
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

LOG = logging.getLogger(__name__)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_BYTES = 32
_MIN_PASSWORD_LENGTH = 12
REALM = "TSUMGI operator console"

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300.0
_MAX_TRACKED_CLIENTS = 1024


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$N$r$p$salt$key`` (base64) for the config file."""
    salt = os.urandom(16) if salt is None else salt
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_BYTES
    )
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(key)}"


def parse_hash(value: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        scheme, n, r, p, salt, key = value.split("$")
        if scheme != "scrypt":
            raise ValueError("scheme")
        parsed = (int(n), int(r), int(p), base64.b64decode(salt, validate=True), base64.b64decode(key, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("admin_ui.password_hash must be made by astrumweaver.control.admin_auth") from exc
    n, r, p, salt, key = parsed
    # Bound the work a config file can ask for, and reject degenerate values.
    if not (2**10 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 8 and salt and key):
        raise ValueError("admin_ui.password_hash has unusable scrypt parameters")
    return parsed


class BasicAuth:
    """Checks ``Authorization: Basic`` against one user name and password hash."""

    def __init__(self, username: str, password_hash: str, *, clock=time.monotonic) -> None:
        if not username or ":" in username or any(ord(c) < 32 for c in username):
            raise ValueError("admin_ui.username must be non-empty text without ':'")
        self._username = username.encode()
        self._n, self._r, self._p, self._salt, self._key = parse_hash(password_hash)
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        # Dependencies run in worker threads; one check at a time also caps
        # the CPU an unauthenticated client can spend on scrypt.
        self._lock = threading.Lock()

    @staticmethod
    def client_id(request: Request) -> str:
        # The nearest proxy appends the peer it saw, so the rightmost entry is
        # the one a client cannot forge.
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.rsplit(",", 1)[-1].strip()
        if last:
            return last[:64]
        return request.client.host if request.client else "unknown"

    def _recent(self, client: str, now: float) -> deque[float]:
        attempts = self._failures.get(client)
        if attempts is None:
            if len(self._failures) >= _MAX_TRACKED_CLIENTS:
                for key in [k for k, v in self._failures.items() if not v or now - v[-1] > FAILURE_WINDOW_SECONDS]:
                    del self._failures[key]
                if len(self._failures) >= _MAX_TRACKED_CLIENTS:
                    self._failures.pop(next(iter(self._failures)))
            attempts = self._failures[client] = deque()
        while attempts and now - attempts[0] > FAILURE_WINDOW_SECONDS:
            attempts.popleft()
        return attempts

    def _credentials(self, header: str) -> tuple[bytes, bytes] | None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token.strip():
            return None
        try:
            decoded = base64.b64decode(token.strip(), validate=True)
        except (ValueError, binascii.Error):
            return None
        user, sep, password = decoded.partition(b":")
        return (user, password) if sep else None

    def _matches(self, user: bytes, password: bytes) -> bool:
        key = hashlib.scrypt(
            password, salt=self._salt, n=self._n, r=self._r, p=self._p, dklen=len(self._key)
        )
        # Both comparisons always run: no early exit on a wrong user name.
        user_ok = hmac.compare_digest(user, self._username)
        key_ok = hmac.compare_digest(key, self._key)
        return user_ok and key_ok

    def __call__(self, request: Request) -> None:
        with self._lock:
            self._check(request)

    def _check(self, request: Request) -> None:
        now = self._clock()
        client = self.client_id(request)
        attempts = self._recent(client, now)
        if len(attempts) >= MAX_FAILURES:
            retry = max(1, int(FAILURE_WINDOW_SECONDS - (now - attempts[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail="ログインに失敗した回数が多すぎます。しばらくしてからやり直してください",
                headers={"Retry-After": str(retry)},
            )
        credentials = self._credentials(request.headers.get("authorization", ""))
        if credentials is not None and self._matches(*credentials):
            attempts.clear()
            return
        # A request with no credentials is the browser's first visit, not a
        # failed guess; only a wrong attempt counts.
        if credentials is not None:
            attempts.append(now)
            LOG.warning("operator console login failed from %s", client)
        raise HTTPException(
            status_code=401,
            detail="認証が必要です",
            headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
        )


def main() -> int:
    """Print a password hash for ``[admin_ui] password_hash``."""
    password = getpass.getpass("Password: ")
    if len(password) < _MIN_PASSWORD_LENGTH:
        print(f"The password must have at least {_MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != password:
        print("The passwords differ.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
