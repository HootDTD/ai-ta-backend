"""Neo4j async client wrapper for ApolloV3 KG storage.

Reads credentials from environment variables only — never hardcoded.
Required env vars: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import AuthError, DatabaseUnavailable, DriverError, TransientError

from apollo.errors import KGUnavailableError

# Degraded-mode exception tuple — Neo4j missing / unreachable / broken
# connection, NOT a Cypher/data-shape bug. Installed neo4j 5.28.3 hierarchy:
# ServiceUnavailable/SessionExpired -> DriverError -> GqlError;
# AuthError -> ClientError -> Neo4jError -> GqlError. There is no common
# ancestor below Exception across DriverError and Neo4jError except GqlError
# (which also covers CypherSyntaxError etc. — those must NOT be swallowed),
# so this tuple names the specific infra-failure branches instead.
KG_DEGRADED_ERRORS: tuple[type[BaseException], ...] = (
    KGUnavailableError,
    DriverError,       # ServiceUnavailable, SessionExpired, session/config errors
    AuthError,         # bad credentials
    TransientError,    # server-side transient
    DatabaseUnavailable,
    OSError,
    asyncio.TimeoutError,
)


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
