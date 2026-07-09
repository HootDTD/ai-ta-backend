"""Neo4j async client wrapper for ApolloV3 KG storage.

Reads credentials from environment variables only — never hardcoded.
Required env vars: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession


class Neo4jClient:
    """Async Neo4j driver wrapper.

    One instance per process (FastAPI app). Yields fresh sessions per request
    via `async with client.session() as s`.
    """

    def __init__(self, uri: str, user: str, password: str, database: str) -> None:
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    @classmethod
    def from_env(cls) -> "Neo4jClient":
        return cls(
            uri=os.environ["NEO4J_URI"],
            user=os.environ["NEO4J_USERNAME"],
            password=os.environ["NEO4J_PASSWORD"],
            database=os.environ["NEO4J_DATABASE"],
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._driver.session(database=self._database) as s:
            yield s

    async def healthcheck(self) -> bool:
        """Returns True if the driver can reach the configured database."""
        async with self.session() as s:
            result = await s.run("RETURN 1 AS ok")
            record = await result.single()
            return record is not None and record["ok"] == 1

    async def close(self) -> None:
        await self._driver.close()
