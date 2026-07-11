"""RED tests for the misconception-detector comparative LLM judge (T5).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.4, amended by A1 (forced JSON confidence field, make_openai_judge is a
NEW direct ``client.chat.completions.create(..., logprobs=True, top_logprobs=5)``
call path — NOT via ``apollo.agent._llm.main_chat``).

Key assertions (per task prompt):
  * stub JudgeFn valid JSON (with confidence field per A1) -> 4-way verdicts
    mapped to findings
  * malformed JSON -> all-clear soft-fail, no raise
  * verdict_token_prob None handled (falls back to per-concept confidence field)
  * one batched call issued for N concepts
  * make_openai_judge logprob-extraction branch covered with a FAKE resp object
    (no network call)
"""

from __future__ import annotations

import json

import pytest

from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.judge import (
    judge_concepts,
    make_openai_judge,
)
from apollo.overseer.misconception_detector.types import (
    JudgeConceptInput,
    JudgeRaw,
)


def _bank_entry(code: str, concept_id: int = 42) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=1,
        concept_id=concept_id,
        code=code,
        description=f"description for {code}",
        confusion_pair=None,
        trigger_phrases=(),
        probe_question="q",
        rt_steps=(),
    )


def _concept(
    key: str = "gdp_identity",
    belief: str = "GDP excludes transfer payments.",
    bank_entries: tuple[MisconceptionEntry, ...] = (),
) -> JudgeConceptInput:
    return JudgeConceptInput(concept_key=key, correct_belief=belief, bank_entries=bank_entries)


class _RecordingJudgeFn:
    """Stub JudgeFn that records call args and returns a canned JudgeRaw."""

    def __init__(self, raw: JudgeRaw) -> None:
        self._raw = raw
        self.calls: list[dict] = []

    def __call__(self, *, system: str, user: str) -> JudgeRaw:
        self.calls.append({"system": system, "user": user})
        return self._raw


class TestJudgeConceptsValidJSON:
    def test_four_way_verdicts_mapped_to_findings(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "clear",
                    "evidence_span": "GDP is C+I+G+NX",
                    "confidence": 0.95,
                },
                {
                    "concept_key": "net_exports_sign",
                    "verdict": "misconception",
                    "evidence_span": "imports add to GDP",
                    "confidence": 0.88,
                },
                {
                    "concept_key": "money_multiplier",
                    "verdict": "needs_clarification",
                    "evidence_span": "not sure about the reserve ratio",
                    "confidence": 0.6,
                },
                {
                    "concept_key": "inflation_target",
                    "verdict": "wrong",
                    "evidence_span": "inflation targeting means zero inflation",
                    "confidence": 0.7,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        concepts = (
            _concept("gdp_identity"),
            _concept("net_exports_sign"),
            _concept("money_multiplier"),
            _concept("inflation_target"),
        )

        findings = judge_concepts(
            problem_text="Explain GDP accounting.",
            concepts=concepts,
            judge_fn=judge_fn,
        )

        assert len(findings) == 4
        by_key = {f.concept_key: f for f in findings}

        assert by_key["gdp_identity"].verdict == "clear"
        assert by_key["gdp_identity"].source == "judge"
        assert by_key["gdp_identity"].confidence == pytest.approx(0.95)

        assert by_key["net_exports_sign"].verdict == "misconception"
        assert by_key["net_exports_sign"].confidence == pytest.approx(0.88)
        assert by_key["net_exports_sign"].evidence_span == "imports add to GDP"

        assert by_key["money_multiplier"].verdict == "needs_clarification"
        assert by_key["money_multiplier"].confidence == pytest.approx(0.6)

        assert by_key["inflation_target"].verdict == "wrong"
        assert by_key["inflation_target"].confidence == pytest.approx(0.7)

        for f in findings:
            assert f.severity == 0.0
            assert f.signature == "unkeyed:" + f.concept_key
            assert f.source == "judge"

    def test_one_batched_call_issued_for_n_concepts(self):
        payload = {
            "concepts": [
                {"concept_key": "a", "verdict": "clear", "evidence_span": "", "confidence": 0.9},
                {"concept_key": "b", "verdict": "clear", "evidence_span": "", "confidence": 0.9},
                {"concept_key": "c", "verdict": "clear", "evidence_span": "", "confidence": 0.9},
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        concepts = (_concept("a"), _concept("b"), _concept("c"))

        findings = judge_concepts(
            problem_text="Problem text",
            concepts=concepts,
            judge_fn=judge_fn,
        )

        assert len(judge_fn.calls) == 1
        assert len(findings) == 3

    def test_verdict_token_prob_used_when_present(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 0.5,  # verbalized field present but should be overridden
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=0.97))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert len(findings) == 1
        assert findings[0].confidence == pytest.approx(0.97)

    def test_verdict_token_prob_none_falls_back_to_verbalized_confidence(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 0.72,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert len(findings) == 1
        assert findings[0].confidence == pytest.approx(0.72)


class TestJudgeConceptsStudentGrounding:
    """Regression tests for the structural-blindness fix: the judge previously
    received ONLY {problem, correct_belief, known_misconceptions} -- zero
    student data -- so it could not possibly ground a verdict in what the
    student actually said. ``judge_concepts`` now accepts
    ``student_utterances`` and threads them into the user prompt as a single
    attempt-level ``student_explanation`` field (not per-concept)."""

    def test_student_utterances_reach_the_user_prompt(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "clear",
                    "evidence_span": "",
                    "confidence": 0.9,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        distinctive = "zebras cause inflation via the reserve multiplier xyzzy-42"

        judge_concepts(
            problem_text="Explain GDP accounting.",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
            student_utterances=(distinctive,),
        )

        assert len(judge_fn.calls) == 1
        user_payload = json.loads(judge_fn.calls[0]["user"])
        assert distinctive in user_payload["student_explanation"]

    def test_multiple_student_utterances_are_joined_attempt_level_not_per_concept(self):
        payload = {
            "concepts": [
                {"concept_key": "a", "verdict": "clear", "evidence_span": "", "confidence": 0.9}
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        utterances = ("first turn utterance", "second turn utterance")

        judge_concepts(
            problem_text="p",
            concepts=(_concept("a"),),
            judge_fn=judge_fn,
            student_utterances=utterances,
        )

        user_payload = json.loads(judge_fn.calls[0]["user"])
        assert "first turn utterance" in user_payload["student_explanation"]
        assert "second turn utterance" in user_payload["student_explanation"]
        # Attempt-level: one shared key, not duplicated per concept row.
        assert "student_explanation" not in user_payload["concepts"][0]

    def test_no_student_utterances_yields_empty_explanation_not_a_crash(self):
        payload = {
            "concepts": [
                {"concept_key": "a", "verdict": "clear", "evidence_span": "", "confidence": 0.9}
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("a"),),
            judge_fn=judge_fn,
        )

        assert len(findings) == 1
        user_payload = json.loads(judge_fn.calls[0]["user"])
        assert user_payload["student_explanation"] == ""

    def test_student_explanation_matching_misconception_keys_to_bank_code(self):
        """End-to-end: a judge row that returns a valid bank code in response
        to (simulated) grounding in the student's explanation still keys to
        misc.<code> -- the new prompt path does not break A11 keying."""
        entry = _bank_entry("includes_transfers")
        concept = _concept("gdp_identity", bank_entries=(entry,))
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "transfers count toward GDP",
                    "confidence": 0.9,
                    "misconception_code": "includes_transfers",
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(
            problem_text="p",
            concepts=(concept,),
            judge_fn=judge_fn,
            student_utterances=("I think transfers count toward GDP",),
        )

        assert len(findings) == 1
        user_payload = json.loads(judge_fn.calls[0]["user"])
        assert "transfers count toward GDP" in user_payload["student_explanation"]
        assert findings[0].bank_code == "includes_transfers"
        assert findings[0].signature == "misc.includes_transfers"
        assert findings[0].verdict == "misconception"


class TestJudgeConceptsSoftFail:
    def test_malformed_json_yields_all_clear_no_raise(self):
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content="not valid json {{{", verdict_token_prob=None)
        )
        concepts = (_concept("gdp_identity"), _concept("net_exports_sign"))

        findings = judge_concepts(
            problem_text="p",
            concepts=concepts,
            judge_fn=judge_fn,
        )

        assert len(findings) == 2
        for f in findings:
            assert f.verdict == "clear"
            assert f.source == "judge"
            assert f.severity == 0.0

    def test_missing_concepts_key_yields_all_clear(self):
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps({"nope": []}), verdict_token_prob=None)
        )
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert len(findings) == 1
        assert findings[0].verdict == "clear"

    def test_judge_fn_raises_yields_all_clear(self):
        def _boom(*, system: str, user: str) -> JudgeRaw:
            raise RuntimeError("network exploded")

        concepts = (_concept("gdp_identity"), _concept("money_multiplier"))
        findings = judge_concepts(
            problem_text="p",
            concepts=concepts,
            judge_fn=_boom,
        )
        assert len(findings) == 2
        assert all(f.verdict == "clear" for f in findings)

    def test_partial_response_missing_concept_row_defaults_clear(self):
        """A row missing entirely from the judge's response for one of the
        requested concepts must not raise or drop that concept — it comes
        back as an unfound/clear finding."""
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 0.9,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        concepts = (_concept("gdp_identity"), _concept("money_multiplier"))
        findings = judge_concepts(
            problem_text="p",
            concepts=concepts,
            judge_fn=judge_fn,
        )
        assert len(findings) == 2
        by_key = {f.concept_key: f for f in findings}
        assert by_key["gdp_identity"].verdict == "misconception"
        assert by_key["money_multiplier"].verdict == "clear"

    def test_empty_concepts_tuple_yields_no_call_no_findings(self):
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps({"concepts": []}), verdict_token_prob=None)
        )
        findings = judge_concepts(problem_text="p", concepts=(), judge_fn=judge_fn)
        assert findings == ()
        assert len(judge_fn.calls) == 0

    def test_confidence_out_of_range_clamped(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 1.5,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].confidence == 1.0

    def test_non_numeric_confidence_defaults_to_zero(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": "not-a-number",
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].confidence == 0.0

    def test_non_string_evidence_span_is_coerced(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": 12345,
                    "confidence": 0.8,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].evidence_span == "12345"

    def test_unknown_verdict_token_soft_fails_to_clear(self):
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "totally_bogus",
                    "evidence_span": "x",
                    "confidence": 0.9,
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].verdict == "clear"


class TestJudgeConceptsFlatCollapseRegression:
    """R2 regression test for the master defect (docs/_archive/experiments/
    2026-07-08-misconception-detector-validation.md): live gpt-4o at
    temperature=0.0 deterministically collapsed the requested top-level
    ``{"concepts": [...]}`` array down to a SINGLE flat object with no
    wrapper, e.g. ``{"concept_key": ..., "verdict": ..., "confidence": ...,
    "evidence_span": ...}``. The old strict ``parsed.get("concepts")`` list
    check failed on this shape every time and silently soft-failed to
    all-clear/confidence-0.0 -- making the whole detector a no-op on real
    data. This must now produce a REAL ConceptFinding, not a soft-fail."""

    def test_real_collapsed_flat_single_object_produces_real_finding(self):
        # Exact shape captured from a live run per the validation writeup:
        # no "concepts" key at all -- just one bare row at the top level.
        payload = {
            "concept_key": "net_exports",
            "verdict": "needs_clarification",
            "evidence_span": "",
            "confidence": 0.5,
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(
            problem_text="Explain net exports in the GDP identity.",
            concepts=(_concept("net_exports", "Net exports = exports - imports."),),
            judge_fn=judge_fn,
        )

        assert len(findings) == 1
        finding = findings[0]
        # Must be the REAL mapped verdict/confidence, not the all-clear
        # soft-fail fallback (verdict="clear", confidence=0.0).
        assert finding.concept_key == "net_exports"
        assert finding.verdict == "needs_clarification"
        assert finding.confidence == pytest.approx(0.5)
        assert finding.source == "judge"
        assert finding.severity == 0.0

    def test_flat_collapse_with_misconception_verdict_is_docked_not_cleared(self):
        payload = {
            "concept_key": "gdp_identity",
            "verdict": "misconception",
            "evidence_span": "imports add to GDP",
            "confidence": 0.88,
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(
            problem_text="Explain GDP accounting.",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )

        assert len(findings) == 1
        assert findings[0].verdict == "misconception"
        assert findings[0].confidence == pytest.approx(0.88)
        assert findings[0].evidence_span == "imports add to GDP"

    def test_flat_collapse_with_multiple_requested_concepts_only_fills_matched_row(self):
        """A flat single-row collapse against a multi-concept batch should
        still be tolerated: the matched concept gets its real finding, and
        any concept the model dropped defaults to the existing per-row
        'clear' fallback (not a full soft-fail of the whole batch)."""
        payload = {
            "concept_key": "gdp_identity",
            "verdict": "wrong",
            "evidence_span": "y",
            "confidence": 0.7,
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        concepts = (_concept("gdp_identity"), _concept("money_multiplier"))

        findings = judge_concepts(
            problem_text="p",
            concepts=concepts,
            judge_fn=judge_fn,
        )

        assert len(findings) == 2
        by_key = {f.concept_key: f for f in findings}
        assert by_key["gdp_identity"].verdict == "wrong"
        assert by_key["gdp_identity"].confidence == pytest.approx(0.7)
        assert by_key["money_multiplier"].verdict == "clear"

    def test_top_level_bare_list_of_rows_is_also_tolerated(self):
        """Belt-and-suspenders: a top-level JSON array (no object wrapper at
        all) of concept rows should also parse, not soft-fail."""
        payload = [
            {"concept_key": "a", "verdict": "clear", "evidence_span": "", "confidence": 0.9},
            {
                "concept_key": "b",
                "verdict": "misconception",
                "evidence_span": "z",
                "confidence": 0.6,
            },
        ]
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("a"), _concept("b")),
            judge_fn=judge_fn,
        )
        assert len(findings) == 2
        by_key = {f.concept_key: f for f in findings}
        assert by_key["a"].verdict == "clear"
        assert by_key["b"].verdict == "misconception"
        assert by_key["b"].confidence == pytest.approx(0.6)

    def test_truly_malformed_shape_still_soft_fails(self):
        """A dict with neither 'concepts' nor concept-row keys must still
        soft-fail cleanly -- the tolerant parse must not become a silent
        pass-through for garbage."""
        payload = {"unrelated_key": "nonsense", "another": 123}
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"), _concept("money_multiplier")),
            judge_fn=judge_fn,
        )
        assert len(findings) == 2
        assert all(f.verdict == "clear" and f.confidence == 0.0 for f in findings)


class TestJsonSchemaShape:
    """R2: locks in the Structured Outputs schema that forces the array
    shape at the API level -- the root cause fix for the master defect."""

    def test_schema_forces_top_level_concepts_array(self):
        from apollo.overseer.misconception_detector.judge import _JSON_SCHEMA

        assert _JSON_SCHEMA["strict"] is True
        schema = _JSON_SCHEMA["schema"]
        assert schema["type"] == "object"
        assert schema["required"] == ["concepts"]
        assert schema["additionalProperties"] is False

        concepts_schema = schema["properties"]["concepts"]
        assert concepts_schema["type"] == "array"

        row_schema = concepts_schema["items"]
        assert row_schema["additionalProperties"] is False
        assert set(row_schema["required"]) == {
            "concept_key",
            "verdict",
            "confidence",
            "evidence_span",
            "misconception_code",
        }
        # confidence must always be present so the verbalized-confidence
        # fallback (A1) always has something to read.
        assert "confidence" in row_schema["properties"]
        assert row_schema["properties"]["verdict"]["enum"] == sorted(
            {"clear", "needs_clarification", "misconception", "wrong"}
        )


class TestJudgeMisconceptionCodeKeying:
    """RED tests for the judge-names-a-code / validated keying redesign (A11):
    docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md
    §4.3, §6.
    """

    def test_judge_names_valid_code_keys_signature(self):
        entry = _bank_entry("includes_transfers")
        concept = _concept("gdp_identity", bank_entries=(entry,))
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "transfers count",
                    "confidence": 0.9,
                    "misconception_code": "includes_transfers",
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(problem_text="p", concepts=(concept,), judge_fn=judge_fn)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.bank_code == "includes_transfers"
        assert finding.signature == "misc.includes_transfers"

    def test_judge_empty_code_stays_unkeyed(self):
        entry = _bank_entry("includes_transfers")
        concept = _concept("gdp_identity", bank_entries=(entry,))
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "clear",
                    "evidence_span": "",
                    "confidence": 0.9,
                    "misconception_code": "",
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(problem_text="p", concepts=(concept,), judge_fn=judge_fn)

        assert len(findings) == 1
        assert findings[0].bank_code is None
        assert findings[0].signature == "unkeyed:gdp_identity"

    def test_judge_hallucinated_code_rejected_unkeyed(self):
        entry = _bank_entry("includes_transfers")
        concept = _concept("gdp_identity", bank_entries=(entry,))
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 0.9,
                    "misconception_code": "totally_made_up",
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(problem_text="p", concepts=(concept,), judge_fn=judge_fn)

        assert len(findings) == 1
        assert findings[0].bank_code is None
        assert findings[0].signature == "unkeyed:gdp_identity"

    def test_judge_cross_concept_code_rejected(self):
        concept_a = _concept("concept_a", bank_entries=(_bank_entry("code_a", concept_id=1),))
        concept_b = _concept("concept_b", bank_entries=(_bank_entry("code_b", concept_id=2),))
        payload = {
            "concepts": [
                {
                    "concept_key": "concept_a",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 0.9,
                    # code_b is valid for concept_b, NOT concept_a.
                    "misconception_code": "code_b",
                },
                {
                    "concept_key": "concept_b",
                    "verdict": "clear",
                    "evidence_span": "",
                    "confidence": 0.9,
                    "misconception_code": "",
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(
            problem_text="p",
            concepts=(concept_a, concept_b),
            judge_fn=judge_fn,
        )

        by_key = {f.concept_key: f for f in findings}
        assert by_key["concept_a"].bank_code is None
        assert by_key["concept_a"].signature == "unkeyed:concept_a"

    def test_judge_missing_code_field_soft_fails_unkeyed(self):
        entry = _bank_entry("includes_transfers")
        concept = _concept("gdp_identity", bank_entries=(entry,))
        payload = {
            "concepts": [
                {
                    "concept_key": "gdp_identity",
                    "verdict": "misconception",
                    "evidence_span": "x",
                    "confidence": 0.9,
                    # misconception_code entirely absent
                },
            ]
        }
        judge_fn = _RecordingJudgeFn(JudgeRaw(content=json.dumps(payload), verdict_token_prob=None))

        findings = judge_concepts(problem_text="p", concepts=(concept,), judge_fn=judge_fn)

        assert len(findings) == 1
        assert findings[0].bank_code is None
        assert findings[0].signature == "unkeyed:gdp_identity"
        assert findings[0].verdict == "misconception"

    def test_judge_all_clear_soft_fail_names_no_code(self):
        entry = _bank_entry("includes_transfers")
        concept = _concept("gdp_identity", bank_entries=(entry,))
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content="not valid json {{{", verdict_token_prob=None)
        )

        findings = judge_concepts(problem_text="p", concepts=(concept,), judge_fn=judge_fn)

        assert len(findings) == 1
        assert findings[0].bank_code is None
        assert findings[0].signature == "unkeyed:gdp_identity"

    def test_judge_schema_includes_misconception_code(self):
        from apollo.overseer.misconception_detector.judge import _JSON_SCHEMA

        row_schema = _JSON_SCHEMA["schema"]["properties"]["concepts"]["items"]
        assert row_schema["properties"]["misconception_code"] == {"type": "string"}
        assert "misconception_code" in row_schema["required"]


class TestMakeOpenAIJudge:
    """Covers the logprob-extraction branch of make_openai_judge with a FAKE
    resp object — no network call is made."""

    def test_returns_callable_judge_fn(self, monkeypatch):
        judge_fn = make_openai_judge(model="gpt-4o")
        assert callable(judge_fn)

    def test_logprob_walk_extracts_verdict_token_prob(self, monkeypatch):
        import math

        import apollo.overseer.misconception_detector.judge as judge_mod

        class _FakeTopLogprob:
            def __init__(self, token, logprob):
                self.token = token
                self.logprob = logprob

        class _FakeTokenLogprob:
            def __init__(self, token, logprob, top_logprobs):
                self.token = token
                self.logprob = logprob
                self.top_logprobs = top_logprobs

        class _FakeLogprobs:
            def __init__(self, content):
                self.content = content

        class _FakeMessage:
            def __init__(self, content):
                self.content = content

        class _FakeChoice:
            def __init__(self, message, logprobs):
                self.message = message
                self.logprobs = logprobs

        class _FakeResp:
            def __init__(self, choices):
                self.choices = choices

        body = json.dumps(
            {
                "concepts": [
                    {
                        "concept_key": "gdp_identity",
                        "verdict": "misconception",
                        "evidence_span": "x",
                        "confidence": 0.5,
                    }
                ]
            }
        )
        verdict_logprob = math.log(0.93)
        fake_content_tokens = [
            _FakeTokenLogprob(
                "misconception",
                verdict_logprob,
                [_FakeTopLogprob("misconception", verdict_logprob)],
            ),
        ]
        fake_resp = _FakeResp(
            choices=[_FakeChoice(_FakeMessage(body), _FakeLogprobs(fake_content_tokens))]
        )

        class _FakeCompletions:
            def create(self, **kwargs):
                assert kwargs.get("logprobs") is True
                assert kwargs.get("top_logprobs") == 5
                response_format = kwargs.get("response_format")
                assert response_format.get("type") == "json_schema"
                json_schema = response_format.get("json_schema", {})
                assert json_schema.get("strict") is True
                assert json_schema.get("schema", {}).get("required") == ["concepts"]
                return fake_resp

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self):
                self.chat = _FakeChat()

        monkeypatch.setattr(judge_mod, "OpenAI", lambda: _FakeClient())

        judge_fn = make_openai_judge(model="gpt-4o")
        raw = judge_fn(system="sys", user="usr")

        assert raw.content == body
        assert raw.verdict_token_prob is not None
        assert raw.verdict_token_prob == pytest.approx(0.93, rel=1e-3)

    def test_logprob_walk_returns_none_when_logprobs_absent(self, monkeypatch):
        import apollo.overseer.misconception_detector.judge as judge_mod

        class _FakeMessage:
            def __init__(self, content):
                self.content = content

        class _FakeChoice:
            def __init__(self, message):
                self.message = message
                self.logprobs = None

        class _FakeResp:
            def __init__(self, choices):
                self.choices = choices

        body = json.dumps({"concepts": []})
        fake_resp = _FakeResp(choices=[_FakeChoice(_FakeMessage(body))])

        class _FakeCompletions:
            def create(self, **kwargs):
                return fake_resp

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self):
                self.chat = _FakeChat()

        monkeypatch.setattr(judge_mod, "OpenAI", lambda: _FakeClient())

        judge_fn = make_openai_judge(model="gpt-4o")
        raw = judge_fn(system="sys", user="usr")

        assert raw.content == body
        assert raw.verdict_token_prob is None

    def test_client_exception_soft_fails_to_empty_content(self, monkeypatch):
        import apollo.overseer.misconception_detector.judge as judge_mod

        class _FakeCompletions:
            def create(self, **kwargs):
                raise RuntimeError("network exploded")

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self):
                self.chat = _FakeChat()

        monkeypatch.setattr(judge_mod, "OpenAI", lambda: _FakeClient())

        judge_fn = make_openai_judge(model="gpt-4o")
        raw = judge_fn(system="sys", user="usr")

        assert raw.verdict_token_prob is None
        assert raw.content == "{}"

    def test_default_model_env_fallback(self, monkeypatch):
        monkeypatch.delenv("MAIN_MODEL", raising=False)
        judge_fn = make_openai_judge()
        assert callable(judge_fn)

    def test_extract_verdict_token_prob_empty_content_list_returns_none(self):
        from apollo.overseer.misconception_detector.judge import (
            _extract_verdict_token_prob,
        )

        class _FakeLogprobs:
            content = []

        class _FakeChoice:
            logprobs = _FakeLogprobs()

        class _FakeResp:
            choices = [_FakeChoice()]

        assert _extract_verdict_token_prob(_FakeResp()) is None

    def test_extract_verdict_token_prob_no_verdict_token_returns_none(self):
        from apollo.overseer.misconception_detector.judge import (
            _extract_verdict_token_prob,
        )

        class _FakeTokenLogprob:
            token = "the"
            logprob = -0.01

        class _FakeLogprobs:
            content = [_FakeTokenLogprob()]

        class _FakeChoice:
            logprobs = _FakeLogprobs()

        class _FakeResp:
            choices = [_FakeChoice()]

        assert _extract_verdict_token_prob(_FakeResp()) is None

    def test_extract_verdict_token_prob_picks_highest_of_multiple_verdict_tokens(self):
        import math

        from apollo.overseer.misconception_detector.judge import (
            _extract_verdict_token_prob,
        )

        class _FakeTokenLogprob:
            def __init__(self, token, logprob):
                self.token = token
                self.logprob = logprob

        class _NoTokenAttr:
            token = None
            logprob = None

        class _FakeLogprobs:
            content = [
                _NoTokenAttr(),
                _FakeTokenLogprob("misconception", math.log(0.9)),
                _FakeTokenLogprob("misconception", math.log(0.6)),
            ]

        class _FakeChoice:
            logprobs = _FakeLogprobs()

        class _FakeResp:
            choices = [_FakeChoice()]

        result = _extract_verdict_token_prob(_FakeResp())
        assert result == pytest.approx(0.9, rel=1e-3)

    def test_extract_verdict_token_prob_malformed_resp_returns_none(self):
        from apollo.overseer.misconception_detector.judge import (
            _extract_verdict_token_prob,
        )

        class _Explodes:
            @property
            def choices(self):
                raise RuntimeError("boom")

        assert _extract_verdict_token_prob(_Explodes()) is None
