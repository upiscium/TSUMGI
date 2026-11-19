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
import ipaddress
import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Collection

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
_MAX_CONCURRENT_VERIFICATIONS = 2
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$N$r$p$salt$key`` (base64) for the config file."""
    salt = os.urandom(16) if salt is None else salt
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_BYTES,
        maxmem=_SCRYPT_MAXMEM,
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
    if not (
        2**10 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 8
        and salt and 16 <= len(key) <= 64
        and 128 * n * r * p <= _SCRYPT_MAXMEM
    ):
        raise ValueError("admin_ui.password_hash has unusable scrypt parameters")
    return parsed


class BasicAuth:
    """Checks ``Authorization: Basic`` against one user name and password hash.

    ``trusted_proxies`` lists the peer addresses allowed to tell us the real
    client in ``X-Forwarded-For`` (the reverse proxy in front of Control).
    From any other peer the header is ignored, so a client that reaches
    Control directly cannot dodge the throttle by forging it.
    """

    def __init__(
        self,
        username: str,
        password_hash: str,
        *,
        trusted_proxies: Collection[str] = (),
        clock=time.monotonic,
    ) -> None:
        if not username or ":" in username or any(ord(c) < 32 for c in username):
            raise ValueError("admin_ui.username must be non-empty text without ':'")
        self._username = username.encode()
        self._n, self._r, self._p, self._salt, self._key = parse_hash(password_hash)
        # Fail at start-up, not on every login, if the parameters cannot run.
        try:
            self._derive(b"")
        except (ValueError, MemoryError) as exc:
            raise ValueError("admin_ui.password_hash has unusable scrypt parameters") from exc
        self._trusted = frozenset(str(p).strip() for p in trusted_proxies if str(p).strip())
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()  # guards _failures only
        # At most a couple of scrypt runs at once; the rest are refused fast
        # instead of queueing in the shared thread pool.
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_VERIFICATIONS)

    def _derive(self, password: bytes) -> bytes:
        return hashlib.scrypt(
            password, salt=self._salt, n=self._n, r=self._r, p=self._p,
            dklen=len(self._key), maxmem=_SCRYPT_MAXMEM,
        )

    def client_id(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if peer in self._trusted:
            # The proxy appends the peer it saw, so the rightmost entry is the
            # one a client cannot forge.
            last = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
            if last:
                peer = last
        return _bucket(peer)

    def _locked_out(self, client: str, now: float) -> int | None:
        """Seconds to wait if the client is locked out, else ``None``."""
        with self._lock:
            attempts = self._failures.get(client)
            if attempts is None:
                return None
            while attempts and now - attempts[0] > FAILURE_WINDOW_SECONDS:
                attempts.popleft()
            if not attempts:
                del self._failures[client]
                return None
            if len(attempts) >= MAX_FAILURES:
                return max(1, int(FAILURE_WINDOW_SECONDS - (now - attempts[0])) + 1)
            return None

    def _record(self, client: str, now: float, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures.pop(client, None)
                return
            if client not in self._failures and len(self._failures) >= _MAX_TRACKED_CLIENTS:
                # Drop expired entries first, then the entry that is closest
                # to expiring; locked-out clients stay as long as possible.
                for key in [k for k, v in self._failures.items() if not v or now - v[-1] > FAILURE_WINDOW_SECONDS]:
                    del self._failures[key]
                if len(self._failures) >= _MAX_TRACKED_CLIENTS:
                    pool = [k for k, v in self._failures.items() if len(v) < MAX_FAILURES] or list(self._failures)
                    del self._failures[min(pool, key=lambda k: self._failures[k][-1])]
            self._failures.setdefault(client, deque()).append(now)

    @staticmethod
    def _credentials(header: str) -> tuple[bytes, bytes] | None:
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
        key = self._derive(password)
        # Both comparisons always run: no early exit on a wrong user name.
        user_ok = hmac.compare_digest(user, self._username)
        key_ok = hmac.compare_digest(key, self._key)
        return user_ok and key_ok

    def __call__(self, request: Request) -> None:
        now = self._clock()
        client = self.client_id(request)
        wait = self._locked_out(client, now)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="ログインに失敗した回数が多すぎます。しばらくしてからやり直してください",
                headers={"Retry-After": str(wait)},
            )
        credentials = self._credentials(request.headers.get("authorization", ""))
        if credentials is not None:
            if not self._slots.acquire(blocking=False):
                raise HTTPException(
                    status_code=429,
                    detail="ログインの処理が混み合っています。少し待ってからやり直してください",
                    headers={"Retry-After": "1"},
                )
            try:
                ok = self._matches(*credentials)
            finally:
                self._slots.release()
            self._record(client, now, ok)
            if ok:
                return
            LOG.warning("operator console login failed from %r", client)
        # A request with no credentials is the browser's first visit, not a
        # failed guess, so it is not counted.
        raise HTTPException(
            status_code=401,
            detail="認証が必要です",
            headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
        )


def _bucket(address: str) -> str:
    """One throttle bucket per client; an IPv6 /64 is one bucket."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address[:64]
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False).network_address)
    return str(parsed)


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
