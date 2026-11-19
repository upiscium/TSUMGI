"""Authentication for the operator console: HTTP Basic with a scrypt hash.

The password is never stored, only a scrypt hash. It comes from one of two
places:

* the Control config (``username`` + ``password_hash``, made with
  ``python -m astrumweaver.control.admin_auth``), or
* a credentials file written by the first-run setup page. Until that file
  exists the console serves only the setup page, and the setup needs a
  one-time code that Control writes to its log and to ``<file>.setup-code``.

Verification is constant time, runs the same work for an unknown user name,
and repeated failures from one client are throttled (the throttle also covers
the right password, so a guessing run cannot tell when it hit). Setup code
guesses use the same throttle.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Collection
from pathlib import Path
from stat import S_ISREG

from fastapi import HTTPException, Request

LOG = logging.getLogger(__name__)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_BYTES = 32
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
REALM = "TSUMGI operator console"

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300.0
_MAX_TRACKED_CLIENTS = 1024
_MAX_CONCURRENT_VERIFICATIONS = 2
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# What the console serves while no password exists yet.
SETUP_GET_PATHS = frozenset({"/admin", "/admin/", "/admin/app.css", "/admin/setup.js"})
SETUP_POST_PATH = "/admin/api/setup"


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$N$r$p$salt$key`` (base64) for the config or credentials file."""
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


def check_username(username: object) -> str:
    if (
        not isinstance(username, str) or not 1 <= len(username) <= 64
        or ":" in username or any(ord(c) < 32 or ord(c) == 127 for c in username)
    ):
        raise ValueError("ユーザー名は 1〜64 文字で、':' と制御文字は使えません")
    try:
        username.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("ユーザー名に使えない文字が含まれています") from None
    return username


def check_password(password: object) -> str:
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"パスワードは {MIN_PASSWORD_LENGTH}〜{MAX_PASSWORD_LENGTH} 文字にしてください")
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("パスワードに使えない文字が含まれています") from None
    return password


class _Verifier:
    """One user name and password hash, checked in constant time."""

    def __init__(self, username: str, password_hash: str) -> None:
        self.username = check_username(username).encode()
        self.n, self.r, self.p, self.salt, self.key = parse_hash(password_hash)
        # Fail when it is loaded, not on every login, if the parameters cannot run.
        try:
            self._derive(b"")
        except (ValueError, MemoryError) as exc:
            raise ValueError("admin_ui.password_hash has unusable scrypt parameters") from exc

    def _derive(self, password: bytes) -> bytes:
        return hashlib.scrypt(
            password, salt=self.salt, n=self.n, r=self.r, p=self.p,
            dklen=len(self.key), maxmem=_SCRYPT_MAXMEM,
        )

    def matches(self, user: bytes, password: bytes) -> bool:
        key = self._derive(password)
        # Both comparisons always run: no early exit on a wrong user name.
        user_ok = hmac.compare_digest(user, self.username)
        key_ok = hmac.compare_digest(key, self.key)
        return user_ok and key_ok


class StaticCredentials:
    """Credentials from the Control config. There is no setup page."""

    needs_setup = False

    def __init__(self, username: str, password_hash: str) -> None:
        self._verifier = _Verifier(username, password_hash)

    def verifier(self) -> _Verifier:
        return self._verifier


def _write_private(directory: Path, text: str) -> Path:
    """A new 0600 file next to the target, never following a planted name."""
    import tempfile

    fd, name = tempfile.mkstemp(dir=directory, prefix=".admin-", suffix=".tmp")  # O_EXCL, 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _publish(tmp: Path, target: Path) -> None:
    """Atomically create ``target`` from ``tmp`` (``FileExistsError`` if it exists)."""
    try:
        try:
            os.link(tmp, target)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            # No hard links on this file system: create the target exclusively.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(tmp.read_text(encoding="utf-8"))
    finally:
        tmp.unlink(missing_ok=True)


def _read_code(path: Path) -> str:
    """The setup code, only from a regular file we own that others cannot read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        stat = os.fstat(handle.fileno())
        if not S_ISREG(stat.st_mode) or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            LOG.error("operator console: ignoring %s (not a private regular file we own)", path)
            return ""
        return handle.read().strip()


class CredentialsFile:
    """Credentials in a file that the setup page writes once.

    The file's existence is checked on every request, so a second Control
    process, or a reset by deleting the file and restarting, behaves correctly.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.code_path = self.path.with_name(self.path.name + ".setup-code")
        self._lock = threading.Lock()
        self._cached: tuple[tuple[int, int], _Verifier] | None = None
        if not self.path.parent.is_dir():
            raise ValueError(f"admin_ui.credentials_file: {self.path.parent} is not a directory")
        if self.needs_setup:
            self._announce_code()
        else:
            self.verifier()  # refuse to start on a file that cannot be used

    @property
    def needs_setup(self) -> bool:
        return not self.path.exists()

    def _announce_code(self) -> None:
        with self._lock:
            code = _read_code(self.code_path)
            if not code:
                raw = secrets.token_hex(10)
                code = "-".join(raw[i:i + 4] for i in range(0, 20, 4))
                tmp = _write_private(self.path.parent, code + "\n")
                try:
                    _publish(tmp, self.code_path)
                except FileExistsError:
                    # Another process (or a stale file we refuse to read) got there first.
                    code = _read_code(self.code_path)
                    if not code:
                        raise ValueError(f"{self.code_path} exists but is not usable; remove it") from None
        LOG.warning(
            "operator console: no password is set yet. Open /admin and enter the setup code %s "
            "(also in %s).", code, self.code_path,
        )

    def verifier(self) -> _Verifier:
        try:
            stat = self.path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            cached = self._cached
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            data = json.loads(self.path.read_text(encoding="utf-8"))
            verifier = _Verifier(data["username"], data["password_hash"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"credentials file {self.path} is unusable") from exc
        self._cached = (fingerprint, verifier)
        return verifier

    def set_up(self, code: str, username: str, password: str) -> bool:
        """Create the credentials. ``False`` means the setup code was wrong.

        Raises ``ValueError`` for a bad user name or password (the code is then
        not consumed) and ``FileExistsError`` if somebody set it up first.
        """
        username = check_username(username)
        password = check_password(password)
        with self._lock:
            if self.path.exists():
                raise FileExistsError(self.path)
            expected = _read_code(self.code_path)
            if not expected or not hmac.compare_digest(code.strip().encode(), expected.encode()):
                return False
            document = json.dumps({"username": username, "password_hash": hash_password(password)})
            _publish(_write_private(self.path.parent, document), self.path)
            try:
                self.code_path.unlink(missing_ok=True)
            except OSError as exc:  # the password is set; do not fail the request
                LOG.error("operator console: could not remove the setup code file: %s", exc)
        LOG.warning("operator console: password set up for user %r; the setup code is now void", username)
        return True


def _bucket(address: str) -> str:
    """One throttle bucket per client; an IPv6 /64 is one bucket."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address[:64]
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False).network_address)
    return str(parsed)


class BasicAuth:
    """FastAPI dependency: ``Authorization: Basic`` against the credentials.

    ``trusted_proxies`` lists the peer addresses allowed to name the real
    client in ``X-Forwarded-For`` (the reverse proxy in front of Control).
    From any other peer the header is ignored, so a client that reaches
    Control directly cannot dodge the throttle by forging it.

    While the credentials need a first-run setup, only the setup page and its
    POST are open; every other path is refused.
    """

    def __init__(
        self,
        credentials: StaticCredentials | CredentialsFile,
        *,
        trusted_proxies: Collection[str] = (),
        clock=time.monotonic,
    ) -> None:
        self._credentials_source = credentials
        self._trusted = frozenset(str(p).strip() for p in trusted_proxies if str(p).strip())
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()  # guards _failures only
        # At most a couple of scrypt runs at once; the rest are refused fast
        # instead of queueing in the shared thread pool.
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_VERIFICATIONS)

    @property
    def needs_setup(self) -> bool:
        return self._credentials_source.needs_setup

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
                # Drop expired entries first, then the entry closest to
                # expiring; locked-out clients stay as long as possible.
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

    def _guard(self, request: Request) -> tuple[str, float]:
        """Refuse locked-out clients; returns the client bucket and the time."""
        now = self._clock()
        client = self.client_id(request)
        wait = self._locked_out(client, now)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="失敗した回数が多すぎます。しばらくしてからやり直してください",
                headers={"Retry-After": str(wait)},
            )
        return client, now

    def _take_slot(self) -> None:
        if not self._slots.acquire(blocking=False):
            raise HTTPException(
                status_code=429,
                detail="処理が混み合っています。少し待ってからやり直してください",
                headers={"Retry-After": "1"},
            )

    def __call__(self, request: Request) -> None:
        if self.needs_setup:
            allowed = (
                (request.method == "GET" and request.url.path in SETUP_GET_PATHS)
                or (request.method == "POST" and request.url.path == SETUP_POST_PATH)
            )
            if allowed:
                return
            raise HTTPException(status_code=403, detail="初期設定が済んでいません。/admin を開いて設定してください")
        client, now = self._guard(request)
        credentials = self._credentials(request.headers.get("authorization", ""))
        if credentials is not None:
            try:
                verifier = self._credentials_source.verifier()
            except ValueError as exc:
                LOG.error("operator console credentials unusable: %s", exc)
                raise HTTPException(status_code=503, detail="認証情報を読み込めません") from exc
            self._take_slot()
            try:
                ok = verifier.matches(*credentials)
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

    def set_up(self, request: Request, code: object, username: object, password: object) -> None:
        """First-run setup. Wrong codes count against the client like wrong passwords."""
        source = self._credentials_source
        if not isinstance(source, CredentialsFile) or not source.needs_setup:
            raise HTTPException(status_code=409, detail="すでに設定済みです")
        client, now = self._guard(request)
        if not isinstance(code, str):
            raise HTTPException(status_code=400, detail="セットアップコードを入力してください")
        self._take_slot()
        try:
            try:
                ok = source.set_up(code, username, password)  # type: ignore[arg-type]
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except FileExistsError as exc:
                raise HTTPException(status_code=409, detail="すでに設定済みです") from exc
            except OSError as exc:
                LOG.error("operator console setup could not write the credentials: %s", exc)
                raise HTTPException(status_code=500, detail="認証情報を保存できません。Control のログを確認してください") from exc
        finally:
            self._slots.release()
        self._record(client, now, ok)
        if not ok:
            LOG.warning("operator console setup code rejected from %r", client)
            raise HTTPException(status_code=403, detail="セットアップコードが違います")


def main() -> int:
    """Print a password hash for ``[admin_ui] password_hash``."""
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != password:
        print("The passwords differ.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
