"""Baseline regeneration — a deliberate act on the BASE commit, never in CI.

Deliberately its OWN module, importing nothing but :mod:`tests.gates_bands.
_baseline` (which in turn imports only the P3.2 harness). The gate module cannot
host this: it imports `score_to_band` / `BAND_TOKENS`, which do not exist on the
base commit, so collecting it there fails before any capture could run — and a
baseline captured anywhere but the base commit records the change as its own
baseline and makes the whole gate vacuous.

To regenerate:

    git archive origin/staging | tar -x -C <scratch>
    cp tests/gates_p32/_harness.py tests/gates_bands/{_baseline,conftest}.py \
       tests/gates_bands/test_capture_baseline.py <scratch>/<same paths>
    cd <scratch> && APOLLO_BANDS_CAPTURE_BASELINE=1 \
       pytest tests/gates_bands/test_capture_baseline.py -q
    cp <scratch>/tests/gates_bands/base_blobs.json tests/gates_bands/

The overlay is required because the base commit has neither this directory nor
the ``topic_score_side_effect`` parameter ``tests/gates_p32/_harness.py`` gained
for the soft-fail cases.
"""

from __future__ import annotations

import os

import pytest

from tests.gates_bands import _baseline

pytestmark = pytest.mark.unit


@pytest.mark.skipif(
    not os.environ.get("APOLLO_BANDS_CAPTURE_BASELINE"),
    reason="baseline capture belongs on the BASE commit; see this module's docstring",
)
async def test_capture_baseline(monkeypatch):
    _baseline.write_baseline(await _baseline.capture(monkeypatch))
