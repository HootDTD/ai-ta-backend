"""Environment hygiene for the proficiency-band gate suite.

The band change rides the SAME Done path the P3.2 §4 gates drive, so this
directory reuses that suite's fixtures verbatim rather than growing a second,
subtly-different notion of "hermetic": the ladder env vars are cleared before
every test, ``transcript_coverage``'s credit-enum latch is re-armed, and any
non-loopback ``socket.connect`` is refused. Importing the fixture functions is
the supported reuse (a non-root ``conftest`` may not declare ``pytest_plugins``).
"""

from __future__ import annotations

from tests.gates_p32.conftest import (  # noqa: F401  (imported for fixture registration)
    _ladder_env,
    _no_network,
    _rearm_credit_enum,
)
