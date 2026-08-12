"""Injectable client seam (S3) and the network guard for offline replay.

Split out of ``campaign/turn_replay.py`` by measurement, not preference: that
module crossed the 800-line file cap, and this is its most self-contained
piece — the plumbing that makes an offline arm offline. Same architecture leaf
(`campaign/turn-replay`), same public API (``turn_replay`` re-exports every
name here), so a caller never has to know about the split.

**These three names ARE the P3.1 Phase 0 contract.** The injected object must
satisfy exactly ``client.chat.completions.create(**kwargs)`` returning an
object with ``choices[0].message.content: str``, and nothing else about it is
assumed — the seam ``unified._call_unified`` documents.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from apollo.agent._llm import bounded_client


class TurnReplayError(RuntimeError):
    """A harness defect — a malformed fixture, or a client asked for more."""


class NetworkBlockedError(TurnReplayError):
    """A non-loopback connect was attempted while the guard was armed."""


# --------------------------------------------------------------------------- #
# Network guard                                                                #
# --------------------------------------------------------------------------- #

_LOOPBACK_HOSTS = frozenset({"::1", "localhost", "localhost.localdomain"})


def _is_loopback(address: Any) -> bool:
    if isinstance(address, str | bytes):
        return True  # AF_UNIX — never leaves the host.
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


@contextmanager
def loopback_only_sockets() -> Iterator[None]:
    """Refuse every non-loopback TCP connect for the duration.

    Playback mode claims "no network". Asserting that by patching the OpenAI
    client only proves the seam we already control; this proves the process. A
    stray call — a lazily-constructed client, a retry inside a vendor SDK, a
    tokenizer download — raises :class:`NetworkBlockedError` instead of quietly
    making a deterministic test depend on the internet.

    Loopback stays open on purpose: the local Postgres/Neo4j containers the
    campaign uses are loopback, and blocking them would make this unusable in
    the one environment it is meant for.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    owned = ("connect" in socket.socket.__dict__, "connect_ex" in socket.socket.__dict__)

    def guard_connect(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(address):
            raise NetworkBlockedError(f"blocked non-loopback connect to {address!r}")
        return real_connect(self, address, *args, **kwargs)

    def guard_connect_ex(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(address):
            raise NetworkBlockedError(f"blocked non-loopback connect_ex to {address!r}")
        return real_connect_ex(self, address, *args, **kwargs)

    socket.socket.connect = guard_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guard_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        if owned[0]:
            socket.socket.connect = real_connect  # type: ignore[method-assign]
        else:
            del socket.socket.connect
        if owned[1]:
            socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        else:
            del socket.socket.connect_ex


# --------------------------------------------------------------------------- #
# S3 client seam                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Message:
    content: str


@dataclass(frozen=True)
class _Choice:
    message: _Message


@dataclass(frozen=True)
class _Completion:
    choices: tuple[_Choice, ...]


class _Completions:
    def __init__(self, owner: ReplayClient) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> _Completion:
        return self._owner._create(kwargs)


class _Chat:
    def __init__(self, owner: ReplayClient) -> None:
        self.completions = _Completions(owner)


class ReplayClient:
    """The S3 shape and nothing more: ``.chat.completions.create(**kwargs)``.

    Both modes record. A LIVE arm's ``recorded`` tuple is exactly the input a
    later PLAYBACK arm replays, which is what makes a live campaign run
    reproducible after the fact.
    """

    def __init__(self) -> None:
        self._recorded: list[str] = []
        self._requests: list[dict[str, Any]] = []
        self.chat = _Chat(self)

    @property
    def recorded(self) -> tuple[str, ...]:
        return tuple(self._recorded)

    @property
    def requests(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._requests)

    @property
    def calls(self) -> int:
        return len(self._recorded)

    def _create(self, kwargs: dict[str, Any]) -> _Completion:
        content = self._respond(kwargs)
        self._requests.append(kwargs)
        self._recorded.append(content)
        return _Completion(choices=(_Choice(message=_Message(content=content)),))

    def _respond(self, kwargs: dict[str, Any]) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class RecordedClient(ReplayClient):
    """PLAYBACK: hand back canned producer JSON, in order, no network.

    ``repeat_last`` covers the regenerate: ``_resolve_served_reply`` may make a
    SECOND call in the same turn (off-policy or malformed draft), and whether it
    does depends on the reconstructed draft, which the caller cannot know ahead
    of time. Repeating the last response is what a fixed model would do, and the
    consequence — the engine exhausts its regenerate and serves the public-clause
    fallback — is a real production path, reported per turn as ``producer_calls``
    and ``fallback_served`` rather than hidden.
    """

    def __init__(self, responses: Sequence[str], *, repeat_last: bool = False) -> None:
        super().__init__()
        self._responses = tuple(responses)
        self._repeat_last = repeat_last
        self._index = 0

    def _respond(self, kwargs: dict[str, Any]) -> str:
        if self._index < len(self._responses):
            value = self._responses[self._index]
            self._index += 1
            return value
        if self._repeat_last and self._responses:
            return self._responses[-1]
        raise TurnReplayError(
            f"recorded client exhausted after {self._index} response(s); "
            "pass repeat_last=True or record the regenerate turn"
        )


class LiveClient(ReplayClient):
    """RECORDING: the real ``bounded_client()``, every raw response kept."""

    def __init__(self, factory: Callable[[], Any] | None = None) -> None:
        super().__init__()
        self._factory = factory if factory is not None else bounded_client
        self._client: Any | None = None

    def _respond(self, kwargs: dict[str, Any]) -> str:
        if self._client is None:
            self._client = self._factory()
        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or "{}"


__all__ = [
    "LiveClient",
    "NetworkBlockedError",
    "RecordedClient",
    "ReplayClient",
    "TurnReplayError",
    "loopback_only_sockets",
]
