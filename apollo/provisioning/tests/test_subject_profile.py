"""Subject-fluid Apollo — profile registry + detection probe + persistence tests.

The registry and the ``detect_profile`` probe are PURE (no DB / LLM), tested
inline. ``persist_profile`` / ``resolve_profile`` touch the ORM, so those use the
real-pgvector ``db_session`` savepoint fixture (Docker-skips cleanly, but the gate
requires GREEN-not-skipped).
"""

from __future__ import annotations

from apollo.provisioning.subject_profile import (
    ALL_GATES,
    DEFAULT_PROFILE_KIND,
    PROFILE_QUALITATIVE_ARGUMENTATIVE,
    PROFILE_QUANTITATIVE_SYMBOLIC,
    ProfileDetection,
    detect_profile,
    get_profile,
    persist_profile,
    resolve_profile,
)

# pytest.ini sets asyncio_mode = auto.


# --------------------------------------------------------------------------- #
# Registry — the two built-ins + fail-open
# --------------------------------------------------------------------------- #


def test_quantitative_symbolic_is_all_eight_gates_symbol_target():
    p = get_profile(PROFILE_QUANTITATIVE_SYMBOLIC)
    assert p.kind == PROFILE_QUANTITATIVE_SYMBOLIC
    assert p.active_gates == ALL_GATES  # gates 1-8
    assert p.target_contract == "symbol"
    assert p.validator == "symbolic"
    # all 6 node types are in the quantitative vocab
    assert {"equation", "simplification", "variable_mapping"} <= p.node_vocab


def test_qualitative_argumentative_is_general_subset_gates_1238_prose():
    p = get_profile(PROFILE_QUALITATIVE_ARGUMENTATIVE)
    assert p.kind == PROFILE_QUALITATIVE_ARGUMENTATIVE
    # the ONLY gates: 1/2/3/8 (4,5 OFF; 6,7 excluded rather than relied on vacuous)
    assert p.active_gates == frozenset({1, 2, 3, 8})
    assert 4 not in p.active_gates and 5 not in p.active_gates
    assert p.target_contract == "prose"
    assert p.validator == "faithfulness"
    # general subset only — no equation / simplification / variable_mapping
    assert p.node_vocab == frozenset({"procedure_step", "definition", "condition"})


def test_get_profile_fails_open_on_none():
    assert get_profile(None).kind == DEFAULT_PROFILE_KIND


def test_get_profile_fails_open_on_unknown_kind():
    # An unknown kind (a corpus tagged with a profile this build doesn't ship)
    # resolves to the strict back-compat default — the safe direction.
    assert get_profile("astrology_vibes").kind == PROFILE_QUANTITATIVE_SYMBOLIC


# --------------------------------------------------------------------------- #
# Detection probe — symbolic vs prose, fail-open, never-raises
# --------------------------------------------------------------------------- #


def _fluid_set() -> list[dict]:
    return [
        {
            "statement": "Water flows through a horizontal pipe at 2.0 m/s; find P2.",
            "given_values": {"v1": 2.0, "P1": 200000.0},
            "reference_solution": [{"entry_type": "equation", "id": "bernoulli"}],
        },
        {
            "statement": "A jet exits a nozzle; compute the exit velocity.",
            "given_values": {"A1": 0.01, "A2": 0.005},
            "reference_solution": [{"entry_type": "equation", "id": "continuity"}],
        },
    ]


def _polisci_set() -> list[dict]:
    return [
        {
            "statement": (
                "Explain why a federal system disperses power across levels of "
                "government, and argue whether this strengthens or weakens "
                "accountability."
            ),
            "solution": (
                "Federalism divides sovereignty between national and subnational "
                "units; this creates multiple veto points which can both check "
                "abuses and blur responsibility."
            ),
        },
        {
            "statement": (
                "Assess the claim that separation of powers is the primary "
                "safeguard against tyranny in a constitutional republic."
            ),
            "solution": (
                "Separation of powers distributes authority so that ambition "
                "counteracts ambition; critics note partisan alignment can erode "
                "the institutional incentive to check a co-partisan branch."
            ),
        },
    ]


def test_detect_profile_symbolic_set_is_quantitative():
    d = detect_profile(_fluid_set())
    assert d.kind == PROFILE_QUANTITATIVE_SYMBOLIC
    assert d.confidence >= 0.6
    assert d.evidence["n_symbolic"] == 2


def test_detect_profile_prose_set_is_qualitative():
    d = detect_profile(_polisci_set())
    assert d.kind == PROFILE_QUALITATIVE_ARGUMENTATIVE
    assert d.confidence >= 0.6
    assert d.evidence["n_prose"] == 2


def test_detect_profile_empty_set_fails_open():
    d = detect_profile([])
    assert d.kind == DEFAULT_PROFILE_KIND
    assert d.confidence == 0.0
    assert d.evidence.get("fail_open") is True


def test_detect_profile_ambiguous_set_fails_open_to_quantitative():
    # 1 symbolic + 1 prose -> prose_fraction 0.5 < 0.6 threshold -> strict default.
    mixed = [_fluid_set()[0], _polisci_set()[0]]
    d = detect_profile(mixed)
    assert d.kind == PROFILE_QUANTITATIVE_SYMBOLIC


def test_detect_profile_never_raises_on_bad_input():
    # Non-mapping items / missing fields must not crash the probe — it filters them
    # out and still returns a valid detection (the never-raises contract). The
    # explicit error fail-open path is covered by the empty-set test above.
    d = detect_profile([None, 42, {"statement": None}])  # type: ignore[list-item]
    assert d.kind in {PROFILE_QUANTITATIVE_SYMBOLIC, PROFILE_QUALITATIVE_ARGUMENTATIVE}
    assert 0.0 <= d.confidence <= 1.0


# --------------------------------------------------------------------------- #
# Persistence — write + read-back over the real ORM
# --------------------------------------------------------------------------- #


async def _seed_subject(db, *, slug: str):
    from apollo.persistence.models import Subject
    from database.models import SearchSpace

    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="X")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    return subj


async def test_new_subject_defaults_to_quantitative_symbolic(db_session):
    """A Subject created without a profile (the existing/ORM path) resolves to the
    back-compat default — the column server_default. This is the hinge that keeps
    every pre-existing provisioning test green."""
    subj = await _seed_subject(db_session, slug="sp-default")
    assert subj.profile_kind == PROFILE_QUANTITATIVE_SYMBOLIC
    profile = await resolve_profile(db_session, subj.id)
    assert profile.kind == PROFILE_QUANTITATIVE_SYMBOLIC
    assert profile.active_gates == ALL_GATES


async def test_persist_then_resolve_qualitative(db_session):
    subj = await _seed_subject(db_session, slug="sp-poli")
    detection = ProfileDetection(
        kind=PROFILE_QUALITATIVE_ARGUMENTATIVE,
        confidence=0.95,
        evidence={"n_problems": 4, "prose_fraction": 0.95},
    )
    await persist_profile(db_session, subj.id, detection)

    refreshed = await db_session.get(type(subj), subj.id)
    assert refreshed.profile_kind == PROFILE_QUALITATIVE_ARGUMENTATIVE
    assert abs(float(refreshed.profile_confidence) - 0.95) < 1e-6
    assert refreshed.profile_evidence["prose_fraction"] == 0.95

    profile = await resolve_profile(db_session, subj.id)
    assert profile.kind == PROFILE_QUALITATIVE_ARGUMENTATIVE
    assert profile.active_gates == frozenset({1, 2, 3, 8})


async def test_resolve_profile_missing_subject_fails_open(db_session):
    # A subject id that does not exist resolves to the strict default, never raises.
    profile = await resolve_profile(db_session, 9_999_999)
    assert profile.kind == PROFILE_QUANTITATIVE_SYMBOLIC
