"""The ladder flag: one reader, clamped, concept-scoped, default OFF.

`APOLLO_WRONGNESS_LEVEL` is an ORDINAL, not a set of booleans, precisely because
the gradient IS the safety design and the kill switch is a single decrement
(spec 2026-08-12 §2.6). Two properties are load-bearing and pinned here:

1. **Level 0 is unreachable-by-accident-proof**: absent, empty, or non-numeric
   reads as 0, and an out-of-range value clamps instead of racing past the
   highest built rung.
2. **Exactly one module reads the env var.** Every gate site in the build calls
   `effective_wrongness_level`; the moment a second reader appears the concept
   allowlist stops being enforced at that site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apollo.overseer import wrongness

pytestmark = pytest.mark.unit

_ENV = "APOLLO_WRONGNESS_LEVEL"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("1", 1),
        ("2", 2),
        ("3", 3),
        ("4", 4),
        ("5", 4),
        ("99", 4),
        ("-1", 0),
        ("-99", 0),
        (" 2 ", 2),
    ],
)
def test_level_clamped_0_to_4(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv(_ENV, raw)
    assert wrongness.effective_wrongness_level("ethics") == expected


@pytest.mark.parametrize("raw", ["", "   ", "on", "true", "2.5", "level-3", "3x", "0x2"])
def test_non_numeric_level_is_zero(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(_ENV, raw)
    assert wrongness.effective_wrongness_level("ethics") == 0


def test_absent_level_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert wrongness.effective_wrongness_level("ethics") == 0
    assert wrongness.effective_wrongness_level(None) == 0


def test_concept_allowlist_forces_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paired with `interaction_allowed_for_concept` exactly like INTERACTION5,
    so the ladder can pilot on one concept without touching anyone else's grade."""
    monkeypatch.setenv(_ENV, "3")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "ethics, thermo")

    assert wrongness.effective_wrongness_level("ethics") == 3
    assert wrongness.effective_wrongness_level("ETHICS") == 3
    assert wrongness.effective_wrongness_level("momentum") == 0
    assert wrongness.effective_wrongness_level(None) == 0


def test_empty_allowlist_means_no_concept_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "2")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    assert wrongness.effective_wrongness_level("anything") == 2
    assert wrongness.effective_wrongness_level(None) == 2


# ---------------------------------------------------------------------------
# S10 — the level-gating table (THE authority for every gate site)
# ---------------------------------------------------------------------------

# (component, the named rung it activates at)
_GATING_TABLE: list[tuple[str, int]] = [
    ("producer schema + WRONGNESS DUTY prompt block", wrongness.LEVEL_PRODUCE),
    ("tagged evidence entry persisted", wrongness.LEVEL_PRODUCE),
    ("adjudicator `contradicted` + coverage['wrongness']", wrongness.LEVEL_PRODUCE),
    ("apollo_wrongness_observed shadow log", wrongness.LEVEL_PRODUCE),
    ("probe priority (L2a)", wrongness.LEVEL_SCHEDULE),
    ("done-gate (L2b)", wrongness.LEVEL_SCHEDULE),
    ("carried challenge (L2c)", wrongness.LEVEL_SCHEDULE),
    ("topics[].misconceptions + narrative + teacher surfaces", wrongness.LEVEL_SURFACE),
    ("XP bonus (+10, decision 7)", wrongness.LEVEL_SURFACE),
]


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_level_gating_table(monkeypatch: pytest.MonkeyPatch, level: int) -> None:
    """Every rung is a SUPERSET of the one below it: a component active at rung
    N is active at every level ≥ N and at none below it. Nothing serves at 0."""
    monkeypatch.setenv(_ENV, str(level))
    effective = wrongness.effective_wrongness_level("ethics")

    for component, activates_at in _GATING_TABLE:
        assert (effective >= activates_at) is (level >= activates_at), component

    if level == 0:
        assert all(effective < activates_at for _, activates_at in _GATING_TABLE)


def test_ceiling_is_the_only_equality_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The level-4 arithmetic is built DARK: `ceiling_active` is `level >= 4`,
    which no deployment sets. Nothing below 4 may apply it."""
    for level in range(0, 5):
        monkeypatch.setenv(_ENV, str(level))
        active = wrongness.effective_wrongness_level("ethics") >= wrongness.LEVEL_CEILING
        assert active is (level == 4)


def test_rungs_are_ordered_and_named() -> None:
    assert (
        wrongness.LEVEL_OFF,
        wrongness.LEVEL_PRODUCE,
        wrongness.LEVEL_SCHEDULE,
        wrongness.LEVEL_SURFACE,
        wrongness.LEVEL_CEILING,
    ) == (0, 1, 2, 3, 4)
    assert wrongness.MAX_LEVEL == wrongness.LEVEL_CEILING


# ---------------------------------------------------------------------------
# Single-reader rule
# ---------------------------------------------------------------------------


def _non_test_sources() -> list[Path]:
    files: list[Path] = []
    for root in ("apollo", "config"):
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            parts = set(path.parts)
            if "tests" in parts or path.name.startswith("test_") or path.name == "conftest.py":
                continue
            files.append(path)
    return files


def test_no_other_module_reads_the_env_var() -> None:
    """One reader, or the concept allowlist silently stops applying at the
    second site. `wrongness.effective_wrongness_level` is the only consumer of
    `config.settings.wrongness_level`, which is the only reader of the env var."""
    readers = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _non_test_sources()
        if _ENV in path.read_text(encoding="utf-8")
    ]
    assert readers == ["config/settings.py"], readers


def test_only_wrongness_calls_the_settings_helper() -> None:
    """`wrongness_level` as a BARE name (the lookbehind excludes the wrapper
    `effective_wrongness_level`, which every gate site is expected to call)."""
    bare_helper = re.compile(r"(?<![\w.])wrongness_level\b")
    callers = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _non_test_sources()
        if bare_helper.search(path.read_text(encoding="utf-8"))
        and path.name not in {"settings.py", "wrongness.py"}
    ]
    assert callers == [], callers
