"""Pure structure-pass fixtures: fake metered chat, no DB and no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

from apollo.provisioning.authored_sets.structure_pass import run_structure_pass


def _chunk(chunk_id: int, content: str):
    return SimpleNamespace(id=chunk_id, content=content)


def _unit(
    kind: str,
    label: str | None,
    start_anchor: str,
    end_anchor: str,
    *,
    confidence: float = 0.95,
) -> dict:
    return {
        "kind": kind,
        "label": label,
        "start_anchor": start_anchor,
        "end_anchor": end_anchor,
        "confidence": confidence,
    }


def _span_text(unit, chunks) -> list[str]:
    by_id = {chunk.id: chunk.content for chunk in chunks}
    return [by_id[span.chunk_id][span.start_char : span.end_char] for span in unit.block_spans]


class _FakeMeteredChat:
    def __init__(self, responses: list[dict], token_costs: list[int] | None = None) -> None:
        self.responses = list(responses)
        self.token_costs = list(token_costs or [100] * len(responses))
        self.tokens = 0
        self.calls: list[dict] = []

    def cumulative_tokens(self) -> int:
        return self.tokens

    def cheap(self, **kwargs) -> str:
        self.calls.append(kwargs)
        assert kwargs["purpose"] == "structure_pass"
        assert kwargs["response_format"]["type"] == "json_schema"
        raw_schema = kwargs["response_format"]["json_schema"]["schema"]["$defs"]["_RawUnit"]
        assert raw_schema["additionalProperties"] is False
        assert set(raw_schema["properties"]) == {
            "kind",
            "label",
            "start_anchor",
            "end_anchor",
            "confidence",
        }
        self.tokens += self.token_costs.pop(0)
        return json.dumps(self.responses.pop(0))


def test_porter_shape_pairs_bare_answer_across_chunks_and_stable_id_order():
    problem = [_chunk(20, "Choose one."), _chunk(10, "1. (MC) Which force?")]
    solution = [
        _chunk(30, "1. (MC) Which force?\nAnswer:"),
        _chunk(31, "Porter's answer spans chunks."),
    ]
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "1.",
                        "1. (MC) Which force?",
                        "Choose one.",
                    )
                ]
            },
            {
                "units": [
                    _unit(
                        "question",
                        "1.",
                        "1. (MC) Which force?",
                        "1. (MC) Which force?",
                    ),
                    _unit(
                        "answer",
                        "1",
                        "Answer:",
                        "Porter's answer spans chunks.",
                    ),
                ]
            },
        ]
    )

    result = run_structure_pass(
        problem_chunks=problem,
        solution_chunks=solution,
        metered_chat=chat,
        scrape_spend=50_000,
    )

    assert [pair.label for pair in result.pairs] == ["1"]
    answer = result.pairs[0].answer
    assert (answer.start_chunk, answer.end_chunk) == (30, 31)
    assert [span.chunk_id for span in answer.block_spans] == [30, 31]
    assert _span_text(answer, solution) == ["Answer:", "Porter's answer spans chunks."]
    assert result.summary().kind_counts == {"question": 2, "answer": 1, "other": 0}
    assert all(call["purpose"] == "structure_pass" for call in chat.calls)
    user_payload = json.loads(chat.calls[0]["messages"][1]["content"])
    assert set(user_payload) == {"document_role", "document"}


def test_keyworded_labels_normalize_and_pair():
    problem_text = "Question 4(a): Find x."
    solution_text = "Solution 4(a): x = 2."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "Question 4(a)",
                        "Question 4(a): Find x.",
                        "Question 4(a): Find x.",
                    )
                ]
            },
            {
                "units": [
                    _unit(
                        "answer",
                        "Solution 4(a)",
                        "Solution 4(a): x = 2.",
                        "Solution 4(a): x = 2.",
                    )
                ]
            },
        ]
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(1, problem_text)],
        solution_chunks=[_chunk(2, solution_text)],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert [pair.label for pair in result.pairs] == ["4a"]


def test_combined_question_answer_document_pairs_within_problem_role():
    text = "Question 2: Why?\nAnswer 2: Because."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "Question 2",
                        "Question 2: Why?",
                        "Question 2: Why?",
                    ),
                    _unit(
                        "answer",
                        "Answer 2",
                        "Answer 2: Because.",
                        "Answer 2: Because.",
                    ),
                ]
            }
        ]
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(7, text)],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert [pair.label for pair in result.pairs] == ["2"]
    assert result.pairs[0].question.document_role == "problem"
    assert result.pairs[0].answer.document_role == "problem"


def test_garbage_document_degrades_to_zero_units_without_raise():
    chat = _FakeMeteredChat([{"units": []}])

    result = run_structure_pass(
        problem_chunks=[_chunk(1, "%%%% not an exam %%%%")],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert result.units == ()
    assert result.pairs == ()
    assert result.summary().unit_count == 0


def test_budget_breach_stops_before_solution_call_and_returns_partial(caplog):
    text = "Question 1: Stop after this document."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "Question 1",
                        "Question 1: Stop after this document.",
                        "Question 1: Stop after this document.",
                    )
                ]
            },
            {"units": []},
        ],
        token_costs=[30_001, 1],
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(1, text)],
        solution_chunks=[_chunk(2, "Answer 1: never called")],
        metered_chat=chat,
        scrape_spend=10,
    )

    assert result.budget_exhausted is True
    assert result.tokens_spent == 30_001
    assert len(result.units) == 1
    assert len(chat.calls) == 1
    assert "authored_set_structure_pass_budget" in caplog.text


def test_ambiguous_labels_are_never_paired():
    problem_text = "Question 1: First.\nQuestion 1: Duplicate."
    solution_text = "Answer 1: One answer."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "1",
                        "Question 1",
                        "First.",
                    ),
                    _unit(
                        "question",
                        "1",
                        "Question 1",
                        "Duplicate.",
                    ),
                ]
            },
            {
                "units": [
                    _unit(
                        "answer",
                        "1",
                        "Answer 1: One answer.",
                        "Answer 1: One answer.",
                    )
                ]
            },
        ]
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(1, problem_text)],
        solution_chunks=[_chunk(2, solution_text)],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert result.pairs == ()
    assert _span_text(result.units[0], [_chunk(1, problem_text)]) == ["Question 1: First."]
    assert _span_text(result.units[1], [_chunk(1, problem_text)]) == ["Question 1: Duplicate."]


def test_anchor_resolution_collapses_whitespace_and_preserves_raw_offsets():
    text = "Prelude.\nQuestion 6: Explain the\nnormal force carefully.\nTrailer."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "6",
                        "Question 6: Explain the normal force",
                        "normal force carefully.",
                    )
                ]
            }
        ]
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(1, text)],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert len(result.units) == 1
    unit = result.units[0]
    assert (unit.start_char, unit.end_char) == (9, 56)
    assert _span_text(unit, [_chunk(1, text)]) == [
        "Question 6: Explain the\nnormal force carefully."
    ]


def test_multichar_whitespace_run_collapses_and_short_or_dangling_anchors_drop():
    text = "Question 9: Sum the  \n  forces acting on the block."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "other",
                        None,
                        "Qu",
                        "ck",
                    ),
                    _unit(
                        "other",
                        None,
                        "Question 9: Sum",
                        "fabricated ending never printed",
                    ),
                    _unit(
                        "question",
                        "9",
                        "Question 9: Sum the forces",
                        "acting on the block.",
                    ),
                ]
            }
        ]
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(1, text)],
        metered_chat=chat,
        scrape_spend=0,
    )

    # The multi-char whitespace run inside the anchor collapses for matching,
    # the raw slice keeps it verbatim; the too-short anchor and the resolved
    # start with a never-printed end anchor each drop only their own unit,
    # leaving the cursor unmoved for the real unit behind them.
    assert len(result.units) == 1
    assert _span_text(result.units[0], [_chunk(1, text)]) == [text]


def test_unresolvable_anchor_drops_only_that_unit_without_logging_document_text(caplog):
    document = "Question 7: Present in the document."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "7",
                        "fabricated opening anchor",
                        "fabricated closing anchor",
                    )
                ]
            }
        ]
    )

    result = run_structure_pass(
        problem_chunks=[_chunk(1, document)],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert result.units == ()
    assert "authored_set_structure_anchor_unresolved" in caplog.text
    assert document not in caplog.text
    record = next(
        record
        for record in caplog.records
        if record.message == "authored_set_structure_anchor_unresolved"
    )
    assert (record.kind, record.label, record.document_role) == ("question", "7", "problem")


def test_garbled_slice_regression_anchors_never_return_shifted_text():
    chunks = [
        _chunk(10, "OCR preface that previously shifted offsets. Answer 8: Use equilibrium"),
        _chunk(11, "and solve for the reaction force. trailing OCR"),
    ]
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "answer",
                        "8",
                        "Answer 8: Use equilibrium",
                        "solve for the reaction force.",
                    )
                ]
            }
        ]
    )

    result = run_structure_pass(
        problem_chunks=chunks,
        metered_chat=chat,
        scrape_spend=0,
    )

    assert len(result.units) == 1
    slices = _span_text(result.units[0], chunks)
    assert slices[0].startswith("Answer 8: Use equilibrium")
    assert slices[-1].endswith("solve for the reaction force.")
    assert all("shifted offsets" not in text for text in slices)


def test_ceiling_headroom_refuses_to_spend_near_the_run_ceiling():
    """The pass shares the run ledger: a run already past half the 2M
    per-document ceiling must get ZERO structure calls (flag=off would have
    survived the rest of the run; the pass must never be what tips it over)."""
    from apollo.provisioning.cost_constants import PER_DOCUMENT_TOKEN_CEILING

    chat = _FakeMeteredChat([{"units": []}])
    chat.tokens = PER_DOCUMENT_TOKEN_CEILING // 2

    result = run_structure_pass(
        problem_chunks=[_chunk(1, "1. A question.")],
        metered_chat=chat,
        scrape_spend=0,
    )

    assert result.budget_exhausted is True
    assert result.units == ()
    assert chat.calls == []
