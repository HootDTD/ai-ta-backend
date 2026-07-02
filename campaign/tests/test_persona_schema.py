"""Unit tests for the D2 persona schema + corpus (campaign/cast/personas/).

Pure filesystem + pydantic checks: loads real JSON on disk (the authored
persona corpus and the real ``apollo/subjects/`` reference data), no DB, no
HTTP, no LLM. This is the "validate with a test that loads the subject data
and asserts every expected key exists" gate the task calls for.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from campaign.cast.personas.schema import (
    CLARIFICATION_POLICIES,
    PERSONA_ARCHETYPES,
    ExpectedLedger,
    PersonaAttempt,
)
from campaign.cast.personas.validate import (
    PROVISIONAL_SUBJECTS,
    iter_persona_files,
    load_persona_file,
    misconception_keys_for,
    reference_keys_for,
    validate_all,
    validate_persona,
)
from campaign.judges.s3_student_fidelity import ledger_vs_expected

pytestmark = pytest.mark.unit

_MINIMAL_KWARGS = dict(
    subject="fluid_mechanics",
    concept="bernoulli_principle",
    problem_id="bernoulli_horizontal_pipe_find_p2",
    system_prompt="teach the pipe problem",
    scripted_beats=["teach continuity"],
    clarification_policy="answer_correctly",
)


# --- schema ----------------------------------------------------------------


def test_expected_ledger_defaults_are_empty():
    ledger = ExpectedLedger()
    assert ledger.credited == []
    assert ledger.unresolved == []
    assert ledger.misconceptions == []
    assert ledger.expects_clarification is False


def test_expected_ledger_rejects_key_in_both_credited_and_unresolved():
    with pytest.raises(ValidationError, match="both credited and unresolved"):
        ExpectedLedger(credited=["eq.bernoulli"], unresolved=["eq.bernoulli"])


def test_persona_attempt_rejects_unknown_archetype():
    with pytest.raises(ValidationError):
        PersonaAttempt(
            persona="bogus",
            expected=ExpectedLedger(credited=["eq.bernoulli"]),
            **_MINIMAL_KWARGS,
        )


def test_persona_attempt_rejects_unknown_clarification_policy():
    kwargs = dict(_MINIMAL_KWARGS)
    kwargs["clarification_policy"] = "shrug"
    with pytest.raises(ValidationError):
        PersonaAttempt(persona="strong", expected=ExpectedLedger(), **kwargs)


def test_persona_attempt_requires_at_least_one_scripted_beat():
    kwargs = dict(_MINIMAL_KWARGS)
    kwargs["scripted_beats"] = []
    with pytest.raises(ValidationError):
        PersonaAttempt(persona="strong", expected=ExpectedLedger(), **kwargs)


def test_vague_persona_must_expect_clarification():
    with pytest.raises(ValidationError, match="expects_clarification=True"):
        PersonaAttempt(
            persona="vague_then_clarifies",
            expected=ExpectedLedger(credited=["eq.bernoulli"], expects_clarification=False),
            **_MINIMAL_KWARGS,
        )


def test_vague_persona_with_expects_clarification_true_is_valid():
    attempt = PersonaAttempt(
        persona="vague_then_clarifies",
        expected=ExpectedLedger(credited=["eq.bernoulli"], expects_clarification=True),
        **_MINIMAL_KWARGS,
    )
    assert attempt.persona == "vague_then_clarifies"


def test_persona_archetypes_and_policies_constants_match_literal():
    assert set(PERSONA_ARCHETYPES) == {
        "strong",
        "partial",
        "misconception",
        "vague_then_clarifies",
    }
    assert set(CLARIFICATION_POLICIES) == {"answer_correctly", "answer_wrong", "stay_vague"}


# --- ExpectedLedger -> S3 dict-shape converter ------------------------------


def test_to_ledger_dict_shape_matches_s3_consumption():
    ledger = ExpectedLedger(
        credited=["eq.continuity", "eq.bernoulli"],
        unresolved=["proc.plan_solve_bernoulli_for_p2"],
        misconceptions=["misc.density_ignored"],
        expects_clarification=False,
    )
    as_dict = ledger.to_ledger_dict()
    assert as_dict == {
        "credited": ["eq.continuity", "eq.bernoulli"],
        "unresolved": ["proc.plan_solve_bernoulli_for_p2"],
        "misconceptions": ["misc.density_ignored"],
    }
    # exercise it exactly the way campaign.judges.s3_student_fidelity does
    actual_ledger = [
        {"key": "eq.continuity", "status": "credited"},
        {"key": "eq.bernoulli", "status": "credited"},
        {"key": "proc.plan_solve_bernoulli_for_p2", "status": "unresolved"},
        {"key": "misc.density_ignored", "status": "misconception"},
    ]
    diff = ledger_vs_expected(actual_ledger, as_dict)
    assert diff["credited"]["agreement"] == 1.0
    assert diff["unresolved"]["agreement"] == 1.0
    assert diff["misconceptions"]["agreement"] == 1.0


def test_to_ledger_dict_only_has_the_three_s3_keys():
    ledger = ExpectedLedger(expects_clarification=True)
    assert set(ledger.to_ledger_dict()) == {"credited", "unresolved", "misconceptions"}


# --- validate.py: real-subject-data cross-check -----------------------------


def test_reference_keys_for_loads_real_bernoulli_problem():
    keys = reference_keys_for(
        "fluid_mechanics", "bernoulli_principle", "bernoulli_horizontal_pipe_find_p2"
    )
    assert "eq.continuity" in keys
    assert "eq.bernoulli" in keys
    assert "cond.incompressibility" in keys


def test_reference_keys_for_unknown_problem_id_raises():
    with pytest.raises(FileNotFoundError):
        reference_keys_for("fluid_mechanics", "bernoulli_principle", "no_such_problem")


def test_misconception_keys_for_loads_real_bank():
    keys = misconception_keys_for("fluid_mechanics", "bernoulli_principle")
    assert "misc.density_ignored" in keys
    assert "misc.pressure_velocity_same_direction" in keys


def test_validate_persona_flags_unknown_credited_key():
    bad = PersonaAttempt(
        persona="strong",
        expected=ExpectedLedger(credited=["eq.does_not_exist"]),
        **_MINIMAL_KWARGS,
    )
    errors = validate_persona(bad)
    assert any("unknown keys" in e for e in errors)


def test_validate_persona_flags_unknown_misconception_key():
    bad = PersonaAttempt(
        persona="misconception",
        expected=ExpectedLedger(credited=["eq.continuity"], misconceptions=["misc.does_not_exist"]),
        **_MINIMAL_KWARGS,
    )
    errors = validate_persona(bad)
    assert any("misconceptions has unknown keys" in e for e in errors)


def test_validate_persona_flags_unknown_unresolved_key():
    bad = PersonaAttempt(
        persona="partial",
        expected=ExpectedLedger(credited=["eq.continuity"], unresolved=["eq.does_not_exist"]),
        **_MINIMAL_KWARGS,
    )
    errors = validate_persona(bad)
    assert any("expected.unresolved has unknown keys" in e for e in errors)


def test_reference_keys_for_missing_problems_dir_raises():
    with pytest.raises(FileNotFoundError, match="no problems/ dir"):
        reference_keys_for("fluid_mechanics", "no_such_concept", "anything")


def test_misconception_keys_for_missing_file_raises():
    with pytest.raises(FileNotFoundError, match="no misconceptions.json"):
        misconception_keys_for("fluid_mechanics", "no_such_concept")


def test_validate_persona_returns_error_when_problem_lookup_fails():
    kwargs = dict(_MINIMAL_KWARGS)
    kwargs["problem_id"] = "no_such_problem_id"
    bad = PersonaAttempt(persona="strong", expected=ExpectedLedger(), **kwargs)
    errors = validate_persona(bad)
    assert len(errors) == 1
    assert "no problem with id" in errors[0]


def test_validate_persona_surfaces_misconception_lookup_failure(monkeypatch):
    import campaign.cast.personas.validate as validate_module

    def _boom(subject, concept):
        raise FileNotFoundError("no misconceptions.json for this concept")

    monkeypatch.setattr(validate_module, "misconception_keys_for", _boom)
    good = PersonaAttempt(
        persona="misconception",
        expected=ExpectedLedger(
            credited=["eq.continuity"], misconceptions=["misc.density_ignored"]
        ),
        **_MINIMAL_KWARGS,
    )
    errors = validate_module.validate_persona(good)
    assert errors == ["no misconceptions.json for this concept"]


def test_iter_persona_files_skips_underscore_prefixed_and_reference_dirs(tmp_path):
    subject_dir = tmp_path / "some_subject"
    subject_dir.mkdir()
    (subject_dir / "_hidden.json").write_text("{}", encoding="utf-8")
    (subject_dir / "strong__p1.json").write_text("{}", encoding="utf-8")
    reference_dir = tmp_path / "some_subject" / "reference" / "concept" / "problems"
    reference_dir.mkdir(parents=True)
    (reference_dir / "p1.json").write_text("{}", encoding="utf-8")

    files = iter_persona_files(base=tmp_path)
    names = {f.name for f in files}
    assert names == {"strong__p1.json"}


def test_validate_all_reports_validation_failure_for_bad_expected_keys(tmp_path):
    subject_dir = tmp_path / "fluid_mechanics"
    subject_dir.mkdir()
    persona = PersonaAttempt(
        persona="strong",
        expected=ExpectedLedger(credited=["eq.does_not_exist"]),
        **_MINIMAL_KWARGS,
    )
    bad_path = subject_dir / "strong__p1.json"
    bad_path.write_text(persona.model_dump_json(), encoding="utf-8")

    failures = validate_all(base=tmp_path)
    assert bad_path in failures
    assert any("unknown keys" in e for e in failures[bad_path])


def test_validate_all_reports_load_failure_for_malformed_json(tmp_path):
    subject_dir = tmp_path / "some_subject"
    subject_dir.mkdir()
    bad_path = subject_dir / "strong__p1.json"
    bad_path.write_text("{not valid json", encoding="utf-8")

    failures = validate_all(base=tmp_path)
    assert bad_path in failures
    assert "failed to load" in failures[bad_path][0]


def test_validate_persona_accepts_real_keys():
    good = PersonaAttempt(
        persona="misconception",
        expected=ExpectedLedger(
            credited=["eq.continuity", "eq.bernoulli"],
            misconceptions=["misc.density_ignored"],
        ),
        **_MINIMAL_KWARGS,
    )
    assert validate_persona(good) == []


# --- the authored corpus itself --------------------------------------------


def test_whole_authored_corpus_validates_clean():
    """Every persona file on disk parses AND its expected keys exist in the
    real (or, for WU-AAS pre-mint subjects, provisional) subject data."""
    failures = validate_all()
    assert failures == {}, failures


def test_corpus_has_all_four_archetypes_per_authored_subject():
    files = iter_persona_files()
    assert files, "expected at least one authored persona file"
    by_subject: dict[str, set[str]] = {}
    for path in files:
        persona = load_persona_file(path)
        by_subject.setdefault(persona.subject, set()).add(persona.persona)
    for subject, archetypes in by_subject.items():
        assert archetypes == set(PERSONA_ARCHETYPES), (
            f"{subject} is missing archetypes: {set(PERSONA_ARCHETYPES) - archetypes}"
        )


def test_incumbent_subjects_have_15_to_25_attempts():
    """Plan D2 / spec §5: 15-25 attempts per subject (brief count here; D3's
    per-attempt paraphrase variation multiplies beyond this at run time)."""
    files = iter_persona_files()
    counts: dict[str, int] = {}
    for path in files:
        persona = load_persona_file(path)
        counts[persona.subject] = counts.get(persona.subject, 0) + 1
    for subject in ("fluid_mechanics", "macroeconomics"):
        assert 15 <= counts[subject] <= 25, (subject, counts[subject])


def test_linear_motion_is_the_only_provisional_subject_authored_so_far():
    # held_out_subject personas are authored in F2 once that subject is
    # minted -- no persona files should exist for it yet.
    files = iter_persona_files()
    subjects_present = {load_persona_file(p).subject for p in files}
    assert subjects_present == {"fluid_mechanics", "macroeconomics", "linear_motion"}
    assert PROVISIONAL_SUBJECTS == frozenset({"linear_motion"})
    assert "held_out_subject" not in subjects_present


def test_every_persona_file_round_trips_through_to_ledger_dict():
    for path in iter_persona_files():
        persona = load_persona_file(path)
        as_dict = persona.expected.to_ledger_dict()
        assert set(as_dict["credited"]) == set(persona.expected.credited)
        assert set(as_dict["unresolved"]) == set(persona.expected.unresolved)
        assert set(as_dict["misconceptions"]) == set(persona.expected.misconceptions)
