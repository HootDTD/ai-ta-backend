"""WU-4A2 Task 3 — soundness pass (S ⊑ R, CONTRADICTIONS ONLY).

RED-first. Binding rules: a contradiction is an S_norm node whose canonical_key
is a misconception key (prefix ``misc.``). Soundness = 1 - penalty(n_contradictions).
**Unsupported extras and unresolved nodes carry ZERO soundness penalty.** Empty
student graph → soundness 1.0 (vacuously sound).
"""

from __future__ import annotations

from apollo.graph_compare.soundness import (
    CONTRADICTION_UNIT_PENALTY,
    EMERGENT_MISCONCEPTION_KEY_PREFIX,
    MISCONCEPTION_KEY_PREFIX,
    contradiction_nodes,
    contradiction_penalty,
    is_misconception_key,
    soundness_score,
)

from ._builders import cnode, empty_snorm, snorm


def test_no_contradiction_soundness_one():
    student = snorm(nodes=(cnode("eq.a"), cnode("cond.incompressibility", node_type="condition")))
    assert soundness_score(student) == 1.0


def test_empty_student_soundness_one_vacuous():
    assert soundness_score(empty_snorm()) == 1.0


def test_single_contradiction_penalized():
    student = snorm(nodes=(cnode("eq.a"), cnode("misc.density_ignored", node_type="misconception")))
    assert soundness_score(student) == 0.5  # 1 - 0.5


def test_two_contradictions_floor_zero():
    student = snorm(
        nodes=(
            cnode("misc.density_ignored", node_type="misconception"),
            cnode("misc.pressure_velocity_same_direction", node_type="misconception"),
        )
    )
    assert soundness_score(student) == 0.0  # penalty capped at 1.0


def test_unsupported_extra_zero_soundness_penalty():
    # A valid assumption the student states that the reference omits -> a
    # non-misconception key not in any path -> unsupported_extra, ZERO penalty.
    student = snorm(nodes=(cnode("cond.steady_flow", node_type="condition"),))
    assert soundness_score(student) == 1.0


def test_unresolved_nodes_zero_soundness_penalty():
    student = snorm(
        nodes=(cnode("eq.a"),),
        unresolved_nodes=(("n_raw", "some garbled surface text"),),
    )
    assert soundness_score(student) == 1.0


def test_misconception_not_in_canon_misc_is_not_contradiction():
    # A wrong-but-unenumerated claim resolves to a NON-misc key -> not a
    # contradiction (honest non-detection; §6.11).
    student = snorm(nodes=(cnode("eq.wrong_but_resolved"),))
    assert contradiction_nodes(student) == ()
    assert soundness_score(student) == 1.0


def test_is_misconception_key_prefix():
    assert is_misconception_key("misc.density_ignored") is True
    assert is_misconception_key("cond.incompressibility") is False
    assert is_misconception_key("misc") is False  # needs the dot
    assert MISCONCEPTION_KEY_PREFIX == "misc."


def test_is_misconception_key_accepts_emergent_prefix():
    """T8/R1: a promoted emergent misconception's own signature
    (``emergent.<entity_key>``) is ALSO a misconception key — it is never
    re-keyed to ``misc.*`` (see candidate_assembly.py / materialize.py)."""
    assert is_misconception_key("emergent.eq.newton2") is True
    assert is_misconception_key("emergent") is False  # needs the dot
    assert EMERGENT_MISCONCEPTION_KEY_PREFIX == "emergent."


def test_emergent_contradiction_penalized_same_as_bank():
    # A resolved emergent-keyed node is a contradiction exactly like a
    # bank-keyed one — same detection path, same penalty math.
    student = snorm(
        nodes=(cnode("eq.a"), cnode("emergent.eq.newton2", node_type="misconception"))
    )
    assert soundness_score(student) == 0.5  # 1 - 0.5, identical to misc.* case


def test_contradiction_nodes_returns_only_misc_nodes():
    misc = cnode("misc.density_ignored", node_type="misconception")
    student = snorm(nodes=(cnode("eq.a"), misc, cnode("eq.b")))
    assert contradiction_nodes(student) == (misc,)


def test_contradiction_penalty_anchors():
    assert contradiction_penalty(0) == 0.0
    assert contradiction_penalty(1) == CONTRADICTION_UNIT_PENALTY
    assert contradiction_penalty(2) == 1.0
    assert contradiction_penalty(5) == 1.0  # capped


def test_empty_bank_soundness_is_na_not_one():
    # D5/D6: empty/absent misconception bank -> N/A (None), NOT vacuous 1.0.
    student = snorm(nodes=(cnode("eq.a"),))
    assert soundness_score(student, bank_applicable=False) is None
    # the SAME student WITH a bank is a real 1.0 (0 contradictions):
    assert soundness_score(student, bank_applicable=True) == 1.0
