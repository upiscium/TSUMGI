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
    connection = EncodingConnection(False, dictionary=True)

    @contextmanager
    def fake_connection(_repo):
        yield connection

    monkeypatch.setattr(PostgresControlRepository, "_connection", fake_connection)
    with pytest.raises(StorageUnavailable, match="UTF8"):
        PostgresControlRepository("postgresql://test").check_storage()
    assert len(connection.statements) == 1
