"""Reject unsupported PostgreSQL encodings before migration mutation (#108)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from astrumweaver.control import migrate
from astrumweaver.control.postgres import PostgresControlRepository
from astrumweaver.control.repository import StorageUnavailable


class EncodingConnection:
    def __init__(self, value, *, dictionary: bool = False):
        self.value = value
        self.dictionary = dictionary
        self.statements: list[str] = []

    def execute(self, sql, *_args, **_kwargs):
        self.statements.append(sql)
        if "server_encoding" not in sql or "client_encoding" not in sql:
            pytest.fail("schema reads/writes may not follow unsupported encoding")
        return self

    def fetchone(self):
        return {"supported": self.value} if self.dictionary else (self.value,)


@pytest.mark.parametrize("value", [False, None, b"t", b"f", "t", 1, 0])
@pytest.mark.parametrize("dictionary", [True, False])
def test_utf8_probe_fails_closed_on_invalid_or_unsupported_encoding(value, dictionary):
    connection = EncodingConnection(value, dictionary=dictionary)
    with pytest.raises(RuntimeError, match="UTF8"):
        migrate._require_utf8_database(connection)
    assert len(connection.statements) == 1


@pytest.mark.parametrize("dictionary", [True, False])
def test_utf8_probe_accepts_only_driver_decoded_true(dictionary):
    connection = EncodingConnection(True, dictionary=dictionary)
    migrate._require_utf8_database(connection)
    assert len(connection.statements) == 1


def test_migration_preflight_refuses_bad_database_before_any_ddl(monkeypatch):
    connection = EncodingConnection(False)

    @contextmanager
    def fake_connect(_dsn, **kwargs):
        assert kwargs == {"autocommit": True}
        yield connection

    monkeypatch.setattr(migrate, "psycopg", SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(migrate, "_migration_files", lambda: ())
    with pytest.raises(RuntimeError, match="UTF8"):
        migrate.apply_migrations("postgresql://test")
    assert len(connection.statements) == 1


def test_control_readiness_refuses_bad_database_before_schema_queries(monkeypatch):
    from astrumweaver.control import postgres as postgres_module

    connection = EncodingConnection(False, dictionary=True)
    closed = []
    connection.close = lambda: closed.append(True)
    monkeypatch.setattr(
        postgres_module, "psycopg",
        SimpleNamespace(connect=lambda _dsn, **_kwargs: connection),
    )
    with pytest.raises(StorageUnavailable, match="UTF8"):
        PostgresControlRepository("postgresql://test").check_storage()
    assert len(connection.statements) == 1
    assert closed == [True]



def test_all_control_requests_refuse_unsupported_encoding_not_only_ready(monkeypatch):
    from astrumweaver.control import postgres as postgres_module

    connection = EncodingConnection(False, dictionary=True)
    closed = []

    def connect(_database_url, *, row_factory):
        assert row_factory is postgres_module.dict_row
        return connection

    connection.close = lambda: closed.append(True)
    monkeypatch.setattr(
        postgres_module, "psycopg", SimpleNamespace(connect=connect),
    )

    repo = PostgresControlRepository("postgresql://test")
    with pytest.raises(StorageUnavailable, match="UTF8"):
        repo.get_worker("probe")

    assert len(connection.statements) == 1
    assert "server_encoding" in connection.statements[0]
    assert closed == [True]



def test_utf8_probe_finishes_implicit_tx_before_explicit_read_committed(monkeypatch):
    """A successful preflight must not preempt the ownership isolation SET."""
    from astrumweaver.control import postgres as postgres_module

    class TxAwareConnection:
        def __init__(self):
            self.phase = "idle"
            self.trace = []

        def execute(self, sql, *_args, **_kwargs):
            if "server_encoding" in sql and "client_encoding" in sql:
                assert self.phase == "idle"
                self.phase = "probe_tx"
                self.trace.append("probe")
            elif "SET TRANSACTION ISOLATION LEVEL READ COMMITTED" in sql:
                assert self.phase == "explicit_tx", (
                    "SET TRANSACTION must run as first statement of fresh transaction"
                )
                self.trace.append("set_transaction")
            else:
                pytest.fail("unexpected SQL in transaction-order fixture")
            return self

        def fetchone(self):
            return {"supported": True}

        def rollback(self):
            assert self.phase == "probe_tx"
            self.trace.append("rollback_probe")
            self.phase = "idle"

        @contextmanager
        def transaction(self):
            assert self.phase == "idle", "probe leaked an implicit transaction"
            self.phase = "explicit_tx"
            self.trace.append("begin_tx")
            try:
                yield self
            finally:
                self.phase = "idle"

        def close(self):
            assert self.phase == "idle"
            self.trace.append("close")

    conn = TxAwareConnection()
    monkeypatch.setattr(
        postgres_module, "psycopg",
        SimpleNamespace(connect=lambda _dsn, **_kwargs: conn),
    )
    repo = PostgresControlRepository("postgresql://test")
    with repo._transaction() as entered:
        assert entered is conn
    assert conn.trace == [
        "probe", "rollback_probe", "begin_tx", "set_transaction", "close",
    ]



def test_utf8_probe_transaction_cleanup_error_denies_database_access(monkeypatch):
    from astrumweaver.control import postgres as postgres_module

    class RollbackFails(EncodingConnection):
        def __init__(self):
            super().__init__(True, dictionary=True)
            self.closed = False

        def rollback(self):
            raise RuntimeError("synthetic rollback unavailable")

        def close(self):
            self.closed = True

    conn = RollbackFails()
    monkeypatch.setattr(
        postgres_module, "psycopg",
        SimpleNamespace(connect=lambda _dsn, **_kwargs: conn),
    )
    with pytest.raises(StorageUnavailable, match="UTF8"):
        PostgresControlRepository("postgresql://test").get_worker("probe")
    assert conn.closed
    assert len(conn.statements) == 1
