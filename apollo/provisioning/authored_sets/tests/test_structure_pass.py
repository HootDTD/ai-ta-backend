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
    start: int,
    end: int,
    *,
    start_chunk: int,
    end_chunk: int,
    confidence: float = 0.95,
) -> dict:
    return {
        "kind": kind,
        "label": label,
        "start_chunk": start_chunk,
        "end_chunk": end_chunk,
        "start_char": start,
        "end_char": end,
        "confidence": confidence,
    }


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
        self.tokens += self.token_costs.pop(0)
        return json.dumps(self.responses.pop(0))


def test_porter_shape_pairs_bare_answer_across_chunks_and_stable_id_order():
    problem = [_chunk(20, "Choose one."), _chunk(10, "1. (MC) Which force?")]
    problem_text = "1. (MC) Which force?\nChoose one."
    solution = [
        _chunk(30, "1. (MC) Which force?\nAnswer:"),
        _chunk(31, "Porter's answer spans chunks."),
    ]
    solution_text = "1. (MC) Which force?\nAnswer:\nPorter's answer spans chunks."
    answer_start = solution_text.index("Answer:")
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "1.",
                        0,
                        len(problem_text),
                        start_chunk=10,
                        end_chunk=20,
                    )
                ]
            },
            {
                "units": [
                    _unit(
                        "question",
                        "1.",
                        0,
                        answer_start,
                        start_chunk=30,
                        end_chunk=30,
                    ),
                    _unit(
                        "answer",
                        "1",
                        answer_start,
                        len(solution_text),
                        start_chunk=30,
                        end_chunk=31,
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
    assert result.summary().kind_counts == {"question": 2, "answer": 1, "other": 0}
    assert all(call["purpose"] == "structure_pass" for call in chat.calls)


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
                        0,
                        len(problem_text),
                        start_chunk=1,
                        end_chunk=1,
                    )
                ]
            },
            {
                "units": [
                    _unit(
                        "answer",
                        "Solution 4(a)",
                        0,
                        len(solution_text),
                        start_chunk=2,
                        end_chunk=2,
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
    answer_start = text.index("Answer")
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit(
                        "question",
                        "Question 2",
                        0,
                        answer_start,
                        start_chunk=7,
                        end_chunk=7,
                    ),
                    _unit(
                        "answer",
                        "Answer 2",
                        answer_start,
                        len(text),
                        start_chunk=7,
                        end_chunk=7,
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
                        0,
                        len(text),
                        start_chunk=1,
                        end_chunk=1,
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
    split = problem_text.index("Question 1", 1)
    solution_text = "Answer 1: One answer."
    chat = _FakeMeteredChat(
        [
            {
                "units": [
                    _unit("question", "1", 0, split, start_chunk=1, end_chunk=1),
                    _unit(
                        "question",
                        "1",
                        split,
                        len(problem_text),
                        start_chunk=1,
                        end_chunk=1,
                    ),
                ]
            },
            {
                "units": [
                    _unit(
                        "answer",
                        "1",
                        0,
                        len(solution_text),
                        start_chunk=2,
                        end_chunk=2,
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
