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

from apollo.overseer.misconception_detector.judge import (
    judge_concepts,
    make_openai_judge,
)
from apollo.overseer.misconception_detector.types import (
    JudgeConceptInput,
    JudgeRaw,
)


def _concept(key: str = "gdp_identity", belief: str = "GDP excludes transfer payments.") -> JudgeConceptInput:
    return JudgeConceptInput(concept_key=key, correct_belief=belief, bank_entries=())


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
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
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
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
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
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=0.97)
        )
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
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert len(findings) == 1
        assert findings[0].confidence == pytest.approx(0.72)


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
                {"concept_key": "gdp_identity", "verdict": "misconception", "evidence_span": "x", "confidence": 0.9},
            ]
        }
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
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
                {"concept_key": "gdp_identity", "verdict": "misconception", "evidence_span": "x", "confidence": 1.5},
            ]
        }
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].confidence == 1.0

    def test_non_numeric_confidence_defaults_to_zero(self):
        payload = {
            "concepts": [
                {"concept_key": "gdp_identity", "verdict": "misconception", "evidence_span": "x", "confidence": "not-a-number"},
            ]
        }
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].confidence == 0.0

    def test_non_string_evidence_span_is_coerced(self):
        payload = {
            "concepts": [
                {"concept_key": "gdp_identity", "verdict": "misconception", "evidence_span": 12345, "confidence": 0.8},
            ]
        }
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].evidence_span == "12345"

    def test_unknown_verdict_token_soft_fails_to_clear(self):
        payload = {
            "concepts": [
                {"concept_key": "gdp_identity", "verdict": "totally_bogus", "evidence_span": "x", "confidence": 0.9},
            ]
        }
        judge_fn = _RecordingJudgeFn(
            JudgeRaw(content=json.dumps(payload), verdict_token_prob=None)
        )
        findings = judge_concepts(
            problem_text="p",
            concepts=(_concept("gdp_identity"),),
            judge_fn=judge_fn,
        )
        assert findings[0].verdict == "clear"


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
            _FakeTokenLogprob("misconception", verdict_logprob, [_FakeTopLogprob("misconception", verdict_logprob)]),
        ]
        fake_resp = _FakeResp(
            choices=[_FakeChoice(_FakeMessage(body), _FakeLogprobs(fake_content_tokens))]
        )

        class _FakeCompletions:
            def create(self, **kwargs):
                assert kwargs.get("logprobs") is True
                assert kwargs.get("top_logprobs") == 5
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
