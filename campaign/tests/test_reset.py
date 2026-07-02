"""Unit tests for campaign.infra.reset — mocked connections, no Docker/DB."""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign.infra.reset import reset_all, reset_neo4j, reset_postgres

pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetch(self, sql):
        return []

    def transaction(self):
        return _FakeTransaction()

    async def close(self):
        self.closed = True


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.asyncio
async def test_reset_postgres_drops_schema_then_bootstraps_and_applies(tmp_path, monkeypatch):
    (tmp_path / "001_one.sql").write_text("-- 001\nSELECT 1;\n", encoding="utf-8")
    conn = _FakeConn()
    baseline_calls: list[str] = []

    async def _connect(dsn):
        return conn

    async def _fake_bootstrap(dsn):
        baseline_calls.append(dsn)

    monkeypatch.setattr("campaign.infra.reset.bootstrap_baseline", _fake_bootstrap)

    applied = await reset_postgres(
        "dsn://ignored", migrations_dir=tmp_path, connect=_connect
    )

    assert any("DROP SCHEMA public CASCADE" in sql for sql in conn.executed)
    assert baseline_calls == ["dsn://ignored"]
    assert applied == ["001_one.sql"]
    assert conn.closed


class _FakeConnNoClose:
    """A connection object with no ``close`` at all (exercises the
    ``close is None`` branch of the finally-block cleanup)."""

    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetch(self, sql):
        return []

    def transaction(self):
        return _FakeTransaction()


class _FakeConnSyncClose:
    """A connection whose ``close`` is synchronous (exercises the
    ``not iscoroutine(result)`` branch)."""

    def __init__(self):
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetch(self, sql):
        return []

    def transaction(self):
        return _FakeTransaction()

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_reset_postgres_handles_connection_with_no_close(tmp_path, monkeypatch):
    (tmp_path / "001_one.sql").write_text("-- 001\nSELECT 1;\n", encoding="utf-8")
    conn = _FakeConnNoClose()

    async def _connect(dsn):
        return conn

    async def _fake_bootstrap(dsn):
        pass

    monkeypatch.setattr("campaign.infra.reset.bootstrap_baseline", _fake_bootstrap)

    applied = await reset_postgres(
        "dsn://ignored", migrations_dir=tmp_path, connect=_connect
    )

    assert applied == ["001_one.sql"]


@pytest.mark.asyncio
async def test_reset_postgres_handles_synchronous_close(tmp_path, monkeypatch):
    (tmp_path / "001_one.sql").write_text("-- 001\nSELECT 1;\n", encoding="utf-8")
    conn = _FakeConnSyncClose()

    async def _connect(dsn):
        return conn

    async def _fake_bootstrap(dsn):
        pass

    monkeypatch.setattr("campaign.infra.reset.bootstrap_baseline", _fake_bootstrap)

    applied = await reset_postgres(
        "dsn://ignored", migrations_dir=tmp_path, connect=_connect
    )

    assert applied == ["001_one.sql"]
    assert conn.closed


@pytest.mark.asyncio
async def test_reset_neo4j_calls_wipe_with_uri_and_auth():
    calls = []

    async def _fake_wipe(uri, database, auth):
        calls.append((uri, database, auth))

    await reset_neo4j(
        "bolt://localhost:57687", ("neo4j", "campaignpass"), wipe=_fake_wipe
    )

    assert calls == [("bolt://localhost:57687", "neo4j", ("neo4j", "campaignpass"))]


@pytest.mark.asyncio
async def test_reset_all_resets_both_stores(tmp_path, monkeypatch):
    (tmp_path / "001_one.sql").write_text("-- 001\nSELECT 1;\n", encoding="utf-8")
    conn = _FakeConn()
    neo4j_calls = []

    async def _connect(dsn):
        return conn

    async def _fake_bootstrap(dsn):
        pass

    async def _fake_wipe(uri, database, auth):
        neo4j_calls.append((uri, database, auth))

    monkeypatch.setattr("campaign.infra.reset.bootstrap_baseline", _fake_bootstrap)

    applied = await reset_all(
        pg_dsn="dsn://ignored",
        neo4j_uri="bolt://localhost:57687",
        neo4j_auth=("neo4j", "campaignpass"),
        migrations_dir=tmp_path,
        pg_connect=_connect,
        neo4j_wipe=_fake_wipe,
    )

    assert applied == ["001_one.sql"]
    assert neo4j_calls == [("bolt://localhost:57687", "neo4j", ("neo4j", "campaignpass"))]
