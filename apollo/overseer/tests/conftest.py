"""Shared fixtures for the overseer test package."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from apollo.overseer import transcript_coverage


@pytest.fixture(autouse=True)
def rearm_credit_enum() -> Iterator[None]:
    """Re-arm the process-level credit-enum latch around every overseer test.

    ``transcript_coverage`` latches the anchored ``credit`` enum OFF for the life
    of the process the first time a provider rejects the schema (bimodal-fix
    P1.1) — deliberate in production, poison in a test session, where one
    downgrade test would silently change the schema every later test sends.
    Resetting on both sides keeps each test independent of ordering, including
    under ``pytest-xdist``.
    """
    transcript_coverage.reset_credit_enum_support()
    yield
    transcript_coverage.reset_credit_enum_support()
