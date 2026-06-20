"""WU-6A2 — the golden-vector heart of the PURE selection + coverage algorithm.

Pure imports — no DB, no LLM, no Neo4j, no network. Every expected number below
is HAND-COMPUTED (the LOCKED golden vectors from the plan, independently
reproduced with the project interpreter on 2026-06-19) — NEVER asserted against
the function's own output. ``EntityProfile``/``LearnerProfile`` come from the
SUBMODULE ``apollo.learner_model.personalization_read`` (frozen WU-6A1, NOT the
package root). ``Problem``/``ReferenceStep`` from ``apollo.schemas.problem``;
``reference_solution_to_entities`` (the FROZEN, DB-free WU-3B conversion core)
from ``apollo.persistence.learner_model_seed``; ``PoolExhaustedError`` from
``apollo.errors``.

LOCKED constants pinned here: TEACHABLE_BAND_LO=0.3, TEACHABLE_BAND_HI=0.7,
MASTERED_THRESHOLD=0.7, UNSEEN_MASTERY=0.50, REPROBE_CONFIDENCE=0.4. Do NOT
re-derive or re-parameterize. Every difficulty-discriminating case pins
``difficulty="intro"`` (the seed has 4 intro / 1 standard / 0 hard).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import apollo
import apollo.learner_model.personalization_select as mod
from apollo.errors import PoolExhaustedError
from apollo.learner_model.personalization_read import EntityProfile, LearnerProfile
from apollo.learner_model.personalization_select import (
    MASTERED_THRESHOLD,
    REPROBE_CONFIDENCE,
    TEACHABLE_BAND_HI,
    TEACHABLE_BAND_LO,
    UNSEEN_MASTERY,
    coverage_score,
    personalize_selection,
    prereqs_mastered,
    reference_entity_keys,
    weak_teachable,
)
from apollo.persistence.learner_model_seed import reference_solution_to_entities
from apollo.schemas.problem import Problem

# reference_solution entry_type -> (entry_type, canonical_key prefix) used by the
# _problem builder to construct a ReferenceStep whose reconstructed key is exactly
# the requested key. Mirrors the FROZEN learner_model_seed map but is LOCAL to the
# test so the builder is independent of the function under test.
_PREFIX_TO_ENTRY_TYPE = {
    "eq": "equation",
    "cond": "condition",
    "simp": "simplification",
    "proc": "procedure_step",
    "def": "definition",
}

SEED_DIR = (
    Path(apollo.__file__).parent / "subjects/fluid_mechanics/concepts/bernoulli_principle/problems"
)


# --- shared builders -----------------------------------------------------------


def _ep(
    key: str,
    mastery: float,
    *,
    entity_id: int,
    confidence: float = 0.8,
    code: str | None = None,
) -> EntityProfile:
    """Construct an EntityProfile directly (no DB)."""
    return EntityProfile(
        entity_id=entity_id,
        canonical_key=key,
        mastery=mastery,
        confidence=confidence,
        misconception_code=code,
    )


def _profile(
    entries: list[EntityProfile],
    *,
    prereq_edges: tuple[tuple[int, int], ...] = (),
    extra_keys: dict[str, int] | None = None,
    is_empty: bool | None = None,
) -> LearnerProfile:
    """Build a LearnerProfile from a list of EntityProfile (no DB).

    Derives the id<->key maps from ``entries`` plus any ``extra_keys`` (canonical
    key -> entity_id) so that prereq endpoints with NO learner_state row (unseen)
    still resolve through the maps, exactly as WU-6A1 guarantees for within-concept
    edges. ``is_empty`` defaults to ``not entries``.
    """
    by_canonical_key = {ep.canonical_key: ep for ep in entries}
    entity_id_by_key: dict[str, int] = {ep.canonical_key: ep.entity_id for ep in entries}
    if extra_keys:
        entity_id_by_key.update(extra_keys)
    key_by_entity_id = {eid: key for key, eid in entity_id_by_key.items()}
    return LearnerProfile(
        by_canonical_key=by_canonical_key,
        prereq_edges=prereq_edges,
        entity_id_by_key=entity_id_by_key,
        key_by_entity_id=key_by_entity_id,
        is_empty=(not entries) if is_empty is None else is_empty,
    )


def _problem(pid: str, difficulty: str, keys: set[str]) -> Problem:
    """Build a minimal-but-VALID Problem whose ``reference_entity_keys`` is exactly
    ``keys``.

    Equation/condition/simplification/definition steps take ``content={"label": ...}``.
    Procedure steps take a contiguous ``order`` and a real ``uses_equations`` ref so
    the Problem ``_resolve_references`` validator passes (needs >=1 equation in the
    problem; we guarantee one by always emitting equations first).
    """
    # Stable order: equations first (so procedure_step.uses_equations can reference
    # a real equation id), then conditions/simplifications/definitions, then procs.
    order_rank = {"eq": 0, "cond": 1, "simp": 1, "def": 1, "proc": 2}
    sorted_keys = sorted(keys, key=lambda k: (order_rank[k.split(".", 1)[0]], k))

    eq_ids = [k.split(".", 1)[1] for k in sorted_keys if k.startswith("eq.")]
    # Every test key set that includes a procedure_step also includes an equation
    # (asserted at construction time below where uses_equations references eq_ids[0]).

    steps: list[dict] = []
    step_no = 1
    proc_order = 1
    for key in sorted_keys:
        prefix, node_id = key.split(".", 1)
        entry_type = _PREFIX_TO_ENTRY_TYPE[prefix]
        if entry_type == "procedure_step":
            assert eq_ids, (
                f"_problem({pid!r}): a procedure_step key needs an equation key in {keys!r}"
            )
            content = {
                "order": proc_order,
                "action": f"do {node_id}",
                "purpose": f"reach target via {node_id}",
                "uses_equations": [eq_ids[0]],
            }
            proc_order += 1
        else:
            content = {"label": node_id}
        steps.append(
            {
                "step": step_no,
                "entry_type": entry_type,
                "id": node_id,
                "content": content,
                "depends_on": [],
            }
        )
        step_no += 1

    return Problem.model_validate(
        {
            "id": pid,
            "concept_id": "bernoulli_principle",
            "difficulty": difficulty,
            "problem_text": f"problem {pid}",
            "given_values": {"x": 1.0},
            "target_unknown": "y",
            "reference_solution": steps,
        }
    )


# The 4 intro seed key sets (worked examples) reproduced as literals.
_P1_KEYS = {
    "cond.incompressibility",
    "eq.bernoulli",
    "eq.continuity",
    "proc.plan_apply_continuity",
    "proc.plan_apply_horizontal_simplification",
    "proc.plan_solve_bernoulli_for_p2",
    "simp.horizontal_simplification",
}
_P2_KEYS = {
    "eq.bernoulli",
    "proc.plan_apply_equal_pressure_simplification",
    "proc.plan_set_v1_zero_and_solve_bernoulli",
    "simp.equal_pressure_simplification",
}
_P3_KEYS = {
    "cond.incompressibility",
    "eq.continuity",
    "proc.plan_invoke_incompressibility",
    "proc.plan_solve_continuity_for_v2",
}
_P4_KEYS = {"eq.flow_rate_definition", "proc.plan_apply_flow_rate_definition"}
_P5_KEYS = {
    "cond.incompressibility",
    "eq.bernoulli",
    "eq.continuity",
    "proc.plan_apply_continuity_for_v2",
    "proc.plan_substitute_into_bernoulli",
}

_P1_ID = "bernoulli_horizontal_pipe_find_p2"
_P2_ID = "bernoulli_height_change_find_v2"
_P3_ID = "continuity_area_change_find_v2"
_P4_ID = "volumetric_flow_rate_find_Q"
_P5_ID = "bernoulli_full_find_p2"


def _intro_pool() -> list[Problem]:
    """The 4 intro problems built via the helper, in sorted-by-id order (as
    ``list_problems_for_concept`` always returns them)."""
    pool = [
        _problem(_P1_ID, "intro", _P1_KEYS),
        _problem(_P2_ID, "intro", _P2_KEYS),
        _problem(_P3_ID, "intro", _P3_KEYS),
        _problem(_P4_ID, "intro", _P4_KEYS),
    ]
    return sorted(pool, key=lambda p: p.id)


# --- 1. LOCKED constant pins ---------------------------------------------------


@pytest.mark.unit
def test_locked_constants():
    assert TEACHABLE_BAND_LO == 0.3
    assert TEACHABLE_BAND_HI == 0.7
    assert MASTERED_THRESHOLD == 0.7
    assert UNSEEN_MASTERY == 0.50
    assert REPROBE_CONFIDENCE == 0.4


# --- 2/3. reference_entity_keys ------------------------------------------------


@pytest.mark.unit
def test_reference_entity_keys_constructed():
    p = _problem("p1", "intro", {"eq.continuity", "cond.incompressibility"})
    assert reference_entity_keys(p) == frozenset({"eq.continuity", "cond.incompressibility"})


@pytest.mark.unit
def test_reference_entity_keys_roundtrip_parity_all_seed_problems():
    """Example E — the make-or-break N+1 disproof. For each real seed file the
    in-memory reconstruction EQUALS both the seeded ``canonical_key``s (via the
    frozen ``reference_solution_to_entities``) AND the raw per-step ``entity_key``.
    """
    for n in range(1, 6):
        raw = json.loads((SEED_DIR / f"problem_0{n}.json").read_text())
        parsed = Problem.model_validate(raw)
        reconstructed = reference_entity_keys(parsed)
        from_seeder = {spec.canonical_key for spec in reference_solution_to_entities(raw)}
        from_raw = {step["entity_key"] for step in raw["reference_solution"]}
        assert reconstructed == from_seeder
        assert reconstructed == from_raw

    # Pin problem_01's expected set as a literal frozenset too.
    raw1 = json.loads((SEED_DIR / "problem_01.json").read_text())
    assert reference_entity_keys(Problem.model_validate(raw1)) == frozenset(
        {
            "cond.incompressibility",
            "eq.bernoulli",
            "eq.continuity",
            "proc.plan_apply_continuity",
            "proc.plan_apply_horizontal_simplification",
            "proc.plan_solve_bernoulli_for_p2",
            "simp.horizontal_simplification",
        }
    )


# --- 4/5/6. prereqs_mastered ---------------------------------------------------


@pytest.mark.unit
def test_prereqs_mastered_no_edges_is_true():
    profile = _profile([_ep("eq.x", 0.4, entity_id=10)])
    assert prereqs_mastered(profile, "eq.x") is True


@pytest.mark.unit
def test_prereqs_mastered_unseen_prereq_blocks():
    """Example D-blocked. continuity (id 1) depends-on entity 2
    (cond.incompressibility) which has NO learner_state row -> unseen 0.50 < 0.70
    -> blocks."""
    profile = _profile(
        [_ep("eq.continuity", 0.40, entity_id=1)],
        prereq_edges=((1, 2),),
        extra_keys={"cond.incompressibility": 2},
    )
    assert prereqs_mastered(profile, "eq.continuity") is False


@pytest.mark.unit
def test_prereqs_mastered_mastered_prereq_admits():
    """Example D-admitted. Same edge (1,2) but entity 2 present at 0.80 >= 0.70."""
    profile = _profile(
        [
            _ep("eq.continuity", 0.40, entity_id=1),
            _ep("cond.incompressibility", 0.80, entity_id=2),
        ],
        prereq_edges=((1, 2),),
    )
    assert prereqs_mastered(profile, "eq.continuity") is True


@pytest.mark.unit
def test_prereqs_mastered_unknown_key_is_true():
    """Defensive: a key absent from the id map has no resolvable prereqs -> True
    (no IO, no raise)."""
    profile = _profile([_ep("eq.x", 0.4, entity_id=10)])
    assert prereqs_mastered(profile, "eq.not_in_map") is True


# --- 7/8/9/10. weak_teachable --------------------------------------------------


@pytest.mark.unit
def test_weak_teachable_band_inclusive_and_deficit():
    profile = _profile(
        [
            _ep("eq.continuity", 0.35, entity_id=1),  # deficit 0.65, in-band
            _ep("eq.bernoulli", 0.68, entity_id=2),  # deficit 0.32, in-band
            _ep("eq.solved", 0.90, entity_id=3),  # out of band (excluded)
            _ep("eq.lo", 0.30, entity_id=4),  # lower bound inclusive -> deficit 0.70
            _ep("eq.hi", 0.70, entity_id=5),  # upper bound inclusive -> deficit 0.30
        ]
    )
    result = weak_teachable(profile)
    assert result == {
        "eq.continuity": pytest.approx(0.65),
        "eq.bernoulli": pytest.approx(0.32),
        "eq.lo": pytest.approx(0.70),
        "eq.hi": pytest.approx(0.30),
    }
    assert "eq.solved" not in result


@pytest.mark.unit
def test_weak_teachable_excludes_prereq_blocked():
    profile = _profile(
        [_ep("eq.continuity", 0.40, entity_id=1)],
        prereq_edges=((1, 2),),
        extra_keys={"cond.incompressibility": 2},
    )
    assert weak_teachable(profile) == {}


@pytest.mark.unit
def test_weak_teachable_softhold_keeps_low_confidence():
    """Example G — soft-hold. Low confidence (< REPROBE_CONFIDENCE) is NEVER a hard
    negative; the entity stays in the weak set with deficit 0.60."""
    profile = _profile(
        [_ep("eq.continuity", 0.40, entity_id=1, confidence=0.2)],
    )
    result = weak_teachable(profile)
    assert result == {"eq.continuity": pytest.approx(0.60)}


@pytest.mark.unit
def test_weak_teachable_empty_on_cold_start():
    assert weak_teachable(_profile([], is_empty=True)) == {}


# --- 11. coverage_score --------------------------------------------------------


@pytest.mark.unit
def test_coverage_score_deficit_weighted():
    weak = {"eq.continuity": 0.65, "cond.incompressibility": 0.60, "eq.bernoulli": 0.32}
    p1 = _problem(_P1_ID, "intro", _P1_KEYS)  # covers all three
    p2 = _problem(_P2_ID, "intro", _P2_KEYS)  # covers only eq.bernoulli
    p4 = _problem(_P4_ID, "intro", _P4_KEYS)  # covers none
    assert coverage_score(p1, weak) == pytest.approx(1.57)
    assert coverage_score(p2, weak) == pytest.approx(0.32)
    assert coverage_score(p4, weak) == pytest.approx(0.0)


# --- 12-19. personalize_selection ----------------------------------------------


@pytest.mark.unit
def test_personalize_selection_deficit_weighted_pick():
    """Example A — the discriminating case. P1=1.57 > P3=1.25 > P2=0.32 > P4=0.0."""
    profile = _profile(
        [
            _ep("eq.continuity", 0.35, entity_id=1),
            _ep("cond.incompressibility", 0.40, entity_id=2),
            _ep("eq.bernoulli", 0.68, entity_id=3),
        ]
    )
    chosen = personalize_selection(
        profile, _intro_pool(), concept_id=7, difficulty="intro", attempted_ids=[]
    )
    assert chosen.id == _P1_ID


@pytest.mark.unit
def test_personalize_selection_tie_break_lowest_id():
    """Example B — tie -> lowest Problem.id, order-independent. Weak is only
    eq.continuity (deficit 0.50): P1 and P3 both cover it -> tie at 0.50; lowest id
    wins (string-min). Run forward AND reversed; identical result."""
    profile = _profile([_ep("eq.continuity", 0.50, entity_id=1)])
    pool = _intro_pool()
    expected = min(_P1_ID, _P3_ID)  # string-min == bernoulli_horizontal_pipe_find_p2
    assert expected == _P1_ID
    forward = personalize_selection(
        profile, pool, concept_id=7, difficulty="intro", attempted_ids=[]
    )
    reverse = personalize_selection(
        profile, list(reversed(pool)), concept_id=7, difficulty="intro", attempted_ids=[]
    )
    assert forward.id == _P1_ID
    assert reverse.id == _P1_ID


@pytest.mark.unit
def test_personalize_selection_cold_start_returns_candidates0():
    """Example C — the non-regression anchor. is_empty profile -> candidates[0],
    byte-identical to today's select_problem branch (sorted-by-id first intro =
    bernoulli_height_change_find_v2, NOT P1)."""
    profile = _profile([], is_empty=True)
    pool = _intro_pool()
    chosen = personalize_selection(
        profile, pool, concept_id=7, difficulty="intro", attempted_ids=[]
    )
    # Pure replication of select_problem's filter branch (do NOT call the async fn).
    replicated = [
        p for p in sorted(pool, key=lambda p: p.id) if p.difficulty == "intro" and p.id not in set()
    ][0]
    assert chosen.id == replicated.id
    assert chosen.id == _P2_ID  # bernoulli_height_change_find_v2


@pytest.mark.unit
def test_personalize_selection_nonempty_profile_empty_weak_returns_candidates0():
    """The ``or not weak`` half of STEP 3: a non-empty profile whose every present
    entity is mastered (>0.7) -> weak == {} -> candidates[0]."""
    profile = _profile(
        [
            _ep("eq.bernoulli", 0.95, entity_id=3),
            _ep("eq.continuity", 0.88, entity_id=1),
        ]
    )
    assert profile.is_empty is False
    assert weak_teachable(profile) == {}
    chosen = personalize_selection(
        profile, _intro_pool(), concept_id=7, difficulty="intro", attempted_ids=[]
    )
    assert chosen.id == _P2_ID  # candidates[0]


@pytest.mark.unit
def test_personalize_selection_partially_warm_unseen_prereq_blocks_to_fallback():
    """Example D end-to-end. Blocked: continuity (0.40) whose only prereq
    (cond.incompressibility, unseen) blocks -> weak == {} -> candidates[0]. Admitted:
    prereq present at 0.80 -> continuity enters weak -> coverage winner (P1/P3,
    lowest-id on tie) NOT candidates[0]."""
    blocked = _profile(
        [_ep("eq.continuity", 0.40, entity_id=1)],
        prereq_edges=((1, 2),),
        extra_keys={"cond.incompressibility": 2},
    )
    chosen_blocked = personalize_selection(
        blocked, _intro_pool(), concept_id=7, difficulty="intro", attempted_ids=[]
    )
    assert chosen_blocked.id == _P2_ID  # candidates[0] (fallback)

    admitted = _profile(
        [
            _ep("eq.continuity", 0.40, entity_id=1),
            _ep("cond.incompressibility", 0.80, entity_id=2),
        ],
        prereq_edges=((1, 2),),
    )
    # weak == {eq.continuity: 0.60}; only P1 and P3 cover eq.continuity -> tie -> P1.
    chosen_admitted = personalize_selection(
        admitted, _intro_pool(), concept_id=7, difficulty="intro", attempted_ids=[]
    )
    assert chosen_admitted.id == _P1_ID
    assert chosen_admitted.id != _P2_ID


@pytest.mark.unit
def test_personalize_selection_pool_exhausted_byte_identical():
    """Example F. No candidate at the requested difficulty -> byte-identical
    PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=...)."""
    full_pool = _intro_pool() + [_problem(_P5_ID, "standard", _P5_KEYS)]
    # Case 1: difficulty='hard' -> zero candidates in the seed mix.
    with pytest.raises(PoolExhaustedError) as exc:
        personalize_selection(
            _profile([], is_empty=True),
            full_pool,
            concept_id=7,
            difficulty="hard",
            attempted_ids=[],
        )
    assert exc.value.concept_cluster_id == "7"
    assert exc.value.difficulty == "hard"
    assert str(exc.value) == "Problem pool exhausted for cluster '7' at difficulty 'hard'"

    # Case 2: all intro candidates attempted -> raises identically.
    with pytest.raises(PoolExhaustedError) as exc2:
        personalize_selection(
            _profile([], is_empty=True),
            _intro_pool(),
            concept_id=7,
            difficulty="intro",
            attempted_ids=[_P1_ID, _P2_ID, _P3_ID, _P4_ID],
        )
    assert exc2.value.concept_cluster_id == "7"
    assert exc2.value.difficulty == "intro"


@pytest.mark.unit
def test_personalize_selection_standard_single_candidate_degeneracy():
    """At difficulty='standard' the seed has exactly ONE candidate (P5); a non-empty
    weak profile still returns P5 (personalization cannot differ with one
    candidate)."""
    full_pool = _intro_pool() + [_problem(_P5_ID, "standard", _P5_KEYS)]
    profile = _profile(
        [
            _ep("eq.continuity", 0.35, entity_id=1),
            _ep("eq.bernoulli", 0.40, entity_id=3),
        ]
    )
    chosen = personalize_selection(
        profile, full_pool, concept_id=7, difficulty="standard", attempted_ids=[]
    )
    assert chosen.id == _P5_ID


@pytest.mark.unit
def test_personalize_selection_respects_attempted_ids_filter():
    """The would-be winner (P1) is attempted -> anti-starvation filter removes it ->
    result is the best of the REMAINING (P3=1.25 on the test-12 weak set)."""
    profile = _profile(
        [
            _ep("eq.continuity", 0.35, entity_id=1),
            _ep("cond.incompressibility", 0.40, entity_id=2),
            _ep("eq.bernoulli", 0.68, entity_id=3),
        ]
    )
    chosen = personalize_selection(
        profile, _intro_pool(), concept_id=7, difficulty="intro", attempted_ids=[_P1_ID]
    )
    assert chosen.id == _P3_ID


# --- 20/21. PURE contract + public surface -------------------------------------


@pytest.mark.unit
def test_no_io_imports():
    src = inspect.getsource(mod)
    for token in ("sqlalchemy", "asyncpg", "openai", "neo4j"):
        assert token not in src, f"personalization_select must not reference {token!r}"


@pytest.mark.unit
def test_public_api_surface():
    expected = {
        "TEACHABLE_BAND_LO",
        "TEACHABLE_BAND_HI",
        "MASTERED_THRESHOLD",
        "UNSEEN_MASTERY",
        "REPROBE_CONFIDENCE",
        "reference_entity_keys",
        "prereqs_mastered",
        "weak_teachable",
        "coverage_score",
        "personalize_selection",
    }
    assert set(mod.__all__) == expected
    for fn in (
        reference_entity_keys,
        prereqs_mastered,
        weak_teachable,
        coverage_score,
        personalize_selection,
    ):
        assert callable(fn)
    assert TEACHABLE_BAND_LO == 0.3
    assert TEACHABLE_BAND_HI == 0.7
    assert MASTERED_THRESHOLD == 0.7
    assert UNSEEN_MASTERY == 0.50
    assert REPROBE_CONFIDENCE == 0.4
