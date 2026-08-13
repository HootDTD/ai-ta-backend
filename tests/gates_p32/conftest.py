"""Environment hygiene for the P3.2 §4 gate suite.

This directory sits under the TOP-LEVEL ``tests/`` tree, so ``apollo/conftest.py``
(which resets the ``INTERACTION*`` env vars around every apollo test and re-arms
``transcript_coverage``'s credit-enum latch) is NOT in scope here. Both are
re-established below, because a leaked ``INTERACTION_CONCEPTS`` would force
``effective_wrongness_level`` to 0 and every level->=1 gate in this suite would
pass for the wrong reason — inert because the ladder never armed, not inert
because the code path is inert.

The loopback socket guard is the third leg of the §4 methodology (the other two
being the recorded adjudicator patch point and the S3 playback client): every
gate here is deterministic ONLY if no seam quietly reaches the network, and a
stray live call would otherwise pass silently as "just slower".
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from apollo.overseer import transcript_coverage

# Env vars whose ambient value could change what a gate observes. Cleared before
# every test; a gate that needs one sets it itself (monkeypatch), never inherits.
_LADDER_ENV = (
    "APOLLO_WRONGNESS_LEVEL",
    "APOLLO_CHALLENGE_BUDGET",
    "INTERACTION1",
    "INTERACTION2",
    "INTERACTION3",
    "INTERACTION4",
    "INTERACTION5",
    "INTERACTION_CONCEPTS",
)

# Everything a hermetic test is allowed to dial. Testcontainers-backed suites do
# not live in this directory; anything else is a defect, not a slow test.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


@pytest.fixture(autouse=True)
def _ladder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LADDER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _rearm_credit_enum() -> Iterator[None]:
    """Mirror ``apollo/overseer/tests/conftest.py``.

    ``transcript_coverage`` latches the anchored ``credit`` enum OFF for the life
    of the process the first time a provider rejects the schema. Deliberate in
    production, poison in a test session — one downgrade test elsewhere would
    silently change the schema every gate here sends.
    """
    transcript_coverage.reset_credit_enum_support()
    yield
    transcript_coverage.reset_credit_enum_support()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any non-loopback ``socket.connect`` for the duration of a gate.

    Patched on ``socket.socket`` (the Python-level class every stdlib and
    third-party client instantiates, including ``http.client`` under the OpenAI
    SDK) rather than on ``_socket.socket``, whose slots are read-only.
    """
    real_connect = socket.socket.connect

    def _guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK_HOSTS:
            raise AssertionError(
                f"P3.2 gate suite attempted a non-loopback connection to {host!r}. "
                "Every LLM seam must be patched (recorded adjudicator / S3 playback "
                "client / hashed narrator) — see tests/gates_p32/_harness.py."
            )
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", _guarded)
