"""The recorded PRE-BAND Done blobs, and the whole-blob diff the gate asserts on.

Spec §A.5's inertness row asks for something the P3.2 harness cannot express on
its own: G-L1 compares two runs of the SAME build (the free variable is
``APOLLO_WRONGNESS_LEVEL``), but the band change has no flag — it is
unconditional — so "vs base" has to mean *vs bytes recorded on the base commit*.
``base_blobs.json`` is exactly that: every blob :class:`~tests.gates_p32.
_harness.GateRun` exposes, captured at ``origin/staging@1f1eafc7`` BEFORE
``score_to_band`` existed, over the same seeded state for every entry in
:data:`CASES` — the four ladder rungs on the topic-score serving branch, plus
the soft-fail branch at the bottom and top rungs. The ladder being part of the
fixture set means the gate also proves the band is inert with respect to the
rung; the soft-fail entries mean it proves the same for the OTHER serving
branch, which the P3.2 harness alone can never reach.

Regenerating it is a deliberate act, never a convenience: check out the base
commit (carrying only this directory and the ``topic_score_side_effect``
parameter ``tests/gates_p32/_harness.py`` gained for it), run
``APOLLO_BANDS_CAPTURE_BASELINE=1 pytest tests/gates_bands/test_gate_bands.py
-k capture``, and commit the result. Doing it on a branch that already carries
the change would record the change as its own baseline and the gate would
assert nothing.

:func:`diff_paths` is the assertion primitive. §A.5 forbids a partial comparison
(the rubric absent-axis hazard hides one rung BELOW the field a narrow assertion
would look at), so nothing here projects or strips: the whole blob is walked and
EVERY differing path is returned, labelled ``added`` / ``removed`` / ``changed``.
The gate then asserts set equality against the enumerated band paths — a diff
the change did not intend cannot be absorbed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.gates_p32._harness import GateRun, run_gate_done


@dataclass(frozen=True)
class Case:
    """One replay of the Done path: a ladder rung on one serving branch."""

    case_id: str
    level: int
    #: True -> ``compute_topic_score`` raises, so ``handle_done`` takes the
    #: SOFT-FAIL serving branch (``_with_band`` over the axis blend) instead of
    #: ``_served_overall_block`` over the topic score.
    soft_fail: bool = False


#: The fixture set. The four ladder rungs on the topic-score branch (the only
#: branch anything else in ``tests/gates_p32/`` can reach, because that harness
#: leaves the real scorer on the path and it always computes), PLUS the soft-fail
#: branch at the bottom and top rungs. That branch is ``_with_band``'s only
#: caller and would otherwise never face a whole-blob comparison. Its most
#: important claim is a NEGATIVE one: ``diagnostic_report.rubric`` — the RAW
#: rubric, persisted as the grade of record and read by every teacher projection
#: — must show ZERO diff, which is what proves the decoration is a copy rather
#: than an in-place mutation.
CASES: tuple[Case, ...] = (
    Case("L0", 0),
    Case("L1", 1),
    Case("L2", 2),
    Case("L3", 3),
    Case("L0-softfail", 0, soft_fail=True),
    Case("L3-softfail", 3, soft_fail=True),
)

#: The forced soft-fail. `_compute_topic_score_safe` swallows it and serves the
#: legacy overall; nothing about the exception itself reaches a blob.
SOFT_FAIL_ERROR = RuntimeError("gate: forced topic-score soft-fail")

#: Every blob a ``GateRun`` exposes that a student or a persisted record can see.
BLOB_NAMES: tuple[str, ...] = (
    "student_response",
    "diagnostic_report",
    "score_details",
    "artifact",
)

BASELINE_PATH = Path(__file__).with_name("base_blobs.json")

#: What the baseline was captured from. Pinned so a stale recapture is visible.
BASELINE_SOURCE = "origin/staging@1f1eafc7 (pre-bands)"

#: The ONE field in these blobs that is a wall clock rather than a grade:
#: ``artifact.grading_latency_ms`` is ``int((time.monotonic() - t0) * 1000)`` and
#: measured 0 / 15 / 16 ms across consecutive runs of the identical input. It is
#: stamped to a sentinel on BOTH sides so the rest of the artifact can be
#: compared whole instead of being dropped from the gate wholesale.
#: :func:`blobs_of` fails loudly if the field ever stops being there, so a rename
#: can never quietly re-admit a live clock into the comparison.
LATENCY_PATH: tuple[str, str] = ("artifact", "grading_latency_ms")
LATENCY_SENTINEL = "<neutralized wall clock>"


def normalize(value: Any) -> Any:
    """Round-trip through canonical JSON, exactly as the stored baseline is.

    Both sides of every comparison go through this, so a gate failure can only
    ever be a real difference and never a ``json.dumps`` coercion artifact.
    """
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def blobs_of(run: GateRun) -> dict[str, Any]:
    """The four normalized blobs of one ``GateRun``, keyed by ``BLOB_NAMES``."""
    blobs = {name: normalize(getattr(run, name)) for name in BLOB_NAMES}
    blob_name, field = LATENCY_PATH
    assert field in blobs[blob_name], (
        f"{blob_name}.{field} is gone — re-check what is non-deterministic in this "
        "blob before editing LATENCY_PATH, or the gate starts comparing a clock."
    )
    blobs[blob_name][field] = LATENCY_SENTINEL
    return blobs


async def run_case(monkeypatch: Any, case: Case) -> GateRun:
    """One ``handle_done`` for ``case``, on the serving branch it selects."""
    return await run_gate_done(
        monkeypatch,
        level=case.level,
        topic_score_side_effect=SOFT_FAIL_ERROR if case.soft_fail else None,
    )


async def capture(monkeypatch: Any) -> dict[str, dict[str, Any]]:
    """Drive every case and return ``{case_id: {blob: value}}``."""
    return {case.case_id: blobs_of(await run_case(monkeypatch, case)) for case in CASES}


def load_baseline() -> dict[str, dict[str, Any]]:
    """The recorded pre-band blobs, keyed by ``Case.case_id``."""
    with open(BASELINE_PATH, encoding="utf-8") as handle:
        document = json.load(handle)
    return document["runs"]


def write_baseline(runs: dict[str, dict[str, Any]]) -> None:
    """Overwrite ``base_blobs.json``. Only ever called by the guarded capture."""
    document = {
        "_source": BASELINE_SOURCE,
        "_what": (
            "Whole Done-path blobs recorded BEFORE the proficiency-band change. "
            "See tests/gates_bands/_baseline.py for how to regenerate."
        ),
        "runs": runs,
    }
    with open(BASELINE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
        handle.write("\n")


def diff_paths(base: Any, live: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Every path at which ``live`` differs from ``base``, deepest-first.

    Returns ``(dotted.path, kind)`` pairs with ``kind`` in
    ``{"added", "removed", "changed"}``. Lists are indexed (``topics[0].credit``)
    and a length change is reported at the list itself rather than as a shower of
    per-index noise. Nothing is skipped, projected, or stripped — that is the
    whole point (§A.5: partial comparisons are forbidden).
    """
    differences: list[tuple[str, str]] = []
    if isinstance(base, dict) and isinstance(live, dict):
        for key in sorted(set(base) | set(live)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in live:
                differences.append((path, "removed"))
            elif key not in base:
                differences.append((path, "added"))
            else:
                differences.extend(diff_paths(base[key], live[key], path))
    elif isinstance(base, list) and isinstance(live, list):
        if len(base) != len(live):
            differences.append((prefix, "changed"))
        else:
            for index, (base_item, live_item) in enumerate(zip(base, live, strict=True)):
                differences.extend(diff_paths(base_item, live_item, f"{prefix}[{index}]"))
    elif base != live:
        differences.append((prefix, "changed"))
    return differences
