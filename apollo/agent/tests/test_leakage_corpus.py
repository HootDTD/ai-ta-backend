"""Static integrity check for the adversarial leakage corpus.

The corpus is a `tests/leakage_corpus.jsonl` of `(draft, history,
kg_summary, expected_leaks)` rows used to grade the LLM-judge against
the policy in `apollo/agent/LEAKAGE_POLICY.md`. Running the full corpus
against the live judge is opt-in (it makes network calls); this module
just checks the file is well-formed and that the pre-filter agrees with
the corpus on the deterministic subset.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apollo.agent.leakage_judge import JudgeVerdict
from apollo.agent.output_filter import validate_or_raise
from apollo.errors import FilterRejectedError
from apollo.subjects import load_concept

_CORPUS_PATH = Path(__file__).parent / "leakage_corpus.jsonl"

_REQUIRED_FIELDS = {"id", "concept_id", "draft", "history", "kg_summary",
                    "expected_leaks", "category"}


def _load_corpus():
    rows = []
    for line in _CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def test_corpus_file_exists():
    assert _CORPUS_PATH.is_file(), f"leakage corpus missing: {_CORPUS_PATH}"


def test_corpus_well_formed():
    rows = _load_corpus()
    assert len(rows) >= 25, "corpus should have at least 25 probes"
    seen_ids: set[str] = set()
    for row in rows:
        missing = _REQUIRED_FIELDS - row.keys()
        assert not missing, f"row {row.get('id')} missing fields: {missing}"
        assert row["id"] not in seen_ids, f"duplicate corpus id {row['id']!r}"
        seen_ids.add(row["id"])
        assert isinstance(row["expected_leaks"], bool)
        assert isinstance(row["history"], list)
        assert isinstance(row["draft"], str) and row["draft"]


def test_corpus_pre_filter_consistency():
    """Where the pre-filter alone is sufficient (verbatim named-law mention),
    it must agree with `expected_leaks=true`. Where `expected_leaks=false`,
    the pre-filter MUST NOT raise (judge stage is what catches paraphrases,
    not the deterministic stage)."""
    rows = _load_corpus()
    concept = load_concept("fluid_mechanics", "bernoulli_principle")

    def _stub_clean(*, draft, concept, history, kg_summary):
        return JudgeVerdict(leaks=False, offending_phrase=None,
                            reason=None, confidence=0.0)

    for row in rows:
        if row["concept_id"] != "bernoulli_principle":
            continue
        history = row["history"]
        summary = row["kg_summary"]
        draft = row["draft"]
        try:
            validate_or_raise(
                draft,
                concept=concept,
                history=history,
                kg_summary=summary,
                judge=_stub_clean,
            )
            pre_filter_rejected = False
        except FilterRejectedError:
            pre_filter_rejected = True

        if not row["expected_leaks"]:
            assert not pre_filter_rejected, (
                f"clean row {row['id']!r} unexpectedly rejected by pre-filter"
            )
        # Note: expected_leaks=true rows MAY be paraphrases the deterministic
        # stage cannot catch — those are judge-only. So we don't assert the
        # pre-filter rejected on every leak row.
