"""Schema + PII gate for the committed turn-replay fixtures (P3.2 W0).

Pure filesystem checks: loads the real JSON under
``campaign/fixtures/turn_replay/``, no DB, no HTTP, no LLM.

Two jobs:

1. **Freeze the schema.** ``campaign/turn_replay.py`` (P3.1 Phase 0 / P3.2 W2-C)
   reads exactly the key set asserted here. A drifting fixture must fail here,
   loudly, rather than in a replay run six waves later.
2. **Enforce the PII scrub as an executable gate.** These are EXACT prod student
   transcripts. Student prose is kept verbatim on purpose (attempt 167's
   self-correction sentence *is* the regression), so the scrub is structural —
   identities, ids and timestamps are dropped — and every retained token is
   reviewed. The capitalized-token allowlist below is deliberately exhaustive:
   any edit that introduces a new proper noun fails until a human reviews it.

See ``campaign/fixtures/turn_replay/README.md`` for provenance, the scrub rules
and the resolution of every sweep hit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apollo.overseer.coverage_contract import BASIS_VALUES
from apollo.overseer.transcript_coverage import _VALID_TALLY_STATES
from apollo.schemas.problem import Problem

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "turn_replay"

# The corpus is capped at 4 attempts on purpose (the 217-attempt prod export is
# NOT committed) — see README §"Why only four".
EXPECTED_FIXTURES = (
    "attempt_083_paragraph_dump.json",
    "attempt_086_zero_transcript.json",
    "attempt_124_conflicting_graded.json",
    "attempt_167_self_correction.json",
)

# --------------------------------------------------------------------------- #
# Frozen schema (W2-C's turn_replay.py reads exactly this)                     #
# --------------------------------------------------------------------------- #

TOP_LEVEL_KEYS = frozenset(
    {
        "fixture_version",
        "attempt_ref",
        "pii_scrubbed",
        "concept",
        "problem",
        "messages",
        "question_opportunities",
        "adjudicator_output",
        "recorded",
    }
)
CONCEPT_KEYS = frozenset({"id", "slug"})
MESSAGE_KEYS = frozenset({"turn_index", "role", "content", "intent"})
QOPP_KEYS = frozenset(
    {
        "reference_node_id",
        "state",
        "times_asked",
        "last_asked_turn",
        "asked_turn",
        "answered_turn",
        "question",
        "evidence",
    }
)
# S2's two-key evidence entry — `done._latest_student_quote` and
# `done._probed_node_ids` read exactly these.
EVIDENCE_KEYS = frozenset({"turn_id", "quote"})
VERDICT_KEYS = frozenset(
    {
        "node_id",
        "covered",
        "credit",
        "confidence",
        "evidence_span",
        "prompted",
        "corrected_later",
        "contradicted",
        "basis",
        "hoot_assisted",
    }
)
RECORDED_KEYS = frozenset({"served_score", "served_letter", "topic_credits"})

ATTEMPT_REF = re.compile(r"^prod-\d{4}-\d{2}-\d{2}/attempt-\d{3}$")

# --------------------------------------------------------------------------- #
# PII sweep                                                                    #
# --------------------------------------------------------------------------- #

# Keys that carry an identity, a database identity, or a wall-clock timestamp.
# None of them may appear anywhere in a fixture, at any depth.
BANNED_KEYS = frozenset(
    {
        "user_id",
        "course_id",
        "session_id",
        "learning_activity_id",
        "attempt_id",
        "problem_id",
        "database_id",
        "created_at",
        "updated_at",
        "storage_key",
        "teacher_upload_id",
        "bucket",
        "metadata",
        "message_metadata",
    }
)

SWEEPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("url", re.compile(r"https?://|\bwww\.[A-Za-z]", re.IGNORECASE)),
    ("handle", re.compile(r"(?<![\w.])@[A-Za-z]\w{2,}")),
    ("phone", re.compile(r"\d{3}[-.\s]?\d{3}[-.\s]?\d{4}")),
    ("digit_run", re.compile(r"\d{7,}")),
    (
        "uuid",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
    ),
    ("timestamp", re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
)

CAPITALIZED_TOKEN = re.compile(r"\b[A-Z][a-z]+\b")
# "Richard O. Mason", "Jacob A. Young", "Robert Wayne Gregory" — 2+ consecutive
# capitalized tokens or initials. This is the shape a real student name takes.
NAME_SEQUENCE = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]\.)(?:\s+(?:[A-Z][a-z]+|[A-Z]\.))+\b")

# Reviewed 2026-08-12. Every one of these is a published academic author named
# in the assigned course reading (Mason's 1986 PAPA paper and the 2020 BIG PAPA
# extension), quoted by the student while teaching. No student, teacher, or
# other private individual is named anywhere in the corpus.
PUBLISHED_AUTHOR_TOKENS = frozenset(
    {
        "Bakici",
        "Gregory",
        "Harminder",
        "Henfridsson",
        "Jacob",
        "Kagan",
        "Mason",
        "Ola",
        "Richard",
        "Robert",
        "Shawn",
        "Singh",
        "Smith",
        "Tyler",
        "Wayne",
        "Young",
        "Zheng",
    }
)

# Course terms (PAPA / BIG PAPA vocabulary, the course-material title Apollo
# cites in its aside) plus ordinary sentence-initial English. Exhaustive by
# construction: a new capitalized token anywhere in the corpus fails the gate.
COURSE_AND_ENGLISH_TOKENS = frozenset(
    {
        "Accessibility",
        "Accucracy",
        "Accuracy",
        "Age",
        "And",
        "Answer",
        "Apollo",
        "At",
        "Because",
        "Before",
        "Behavioral",
        "Behaviour",
        "Big",
        "But",
        "Can",
        "Data",
        "Denial",
        "Discrimination",
        "Do",
        "Each",
        "Enumerating",
        "Errors",
        "Especially",
        "Ethical",
        "Ethics",
        "Explain",
        "Exposure",
        "For",
        "Forced",
        "Four",
        "Got",
        "Governance",
        "He",
        "How",
        "If",
        "In",
        "Information",
        "Intellectual",
        "Interpretation",
        "Issues",
        "It",
        "Its",
        "Link",
        "Lose",
        "Loss",
        "Manipulation",
        "Map",
        "Module",
        "Okay",
        "Privacy",
        "Property",
        "Proposed",
        "Reliance",
        "Show",
        "Slides",
        "So",
        "Surveillance",
        "Thanks",
        "That",
        "The",
        "There",
        "Ties",
        "To",
        "Uncompensated",
        "What",
        "When",
        "Why",
        "With",
        "You",
    }
)
REVIEWED_TOKENS = PUBLISHED_AUTHOR_TOKENS | COURSE_AND_ENGLISH_TOKENS

# Reviewed 2026-08-12: 7 published-author names + 10 course-term / ordinary
# phrase collisions. Nothing here identifies a private individual.
REVIEWED_NAME_SEQUENCES = frozenset(
    {
        "Behavioral Surveillance",
        "Big Data",
        "Ethics Slides",
        "Forced Exposure",
        "Four Ethical Issues",
        "Harminder Singh",
        "Information Age",
        "Jacob A. Young",
        "Kagan Bakici",
        "Ola Henfridsson",
        "Richard Mason",
        "Richard O. Mason",
        "Robert Wayne Gregory",
        "Shawn H. Zheng",
        "Tyler J. Smith",
        "Uncompensated Loss",
        "When Mason",
    }
)

# Attempt 167's regression content. Paraphrasing it destroys the fixture: S2'
# must NOT select this node because the student corrected himself in dialogue.
SELF_CORRECTION_QUOTE = "I was wrong about governance"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _iter_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_keys(item)


@pytest.fixture(params=EXPECTED_FIXTURES)
def fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    return _load(request.param)


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #


def test_fixture_directory_holds_exactly_the_four_curated_attempts() -> None:
    """Capped at 4 — the 217-attempt prod export is never committed."""
    assert sorted(p.name for p in FIXTURE_DIR.glob("*.json")) == sorted(EXPECTED_FIXTURES)


def test_every_fixture_parses_as_problem(fixture: dict[str, Any]) -> None:
    problem = Problem.model_validate(fixture["problem"])
    graph = problem.to_kg_graph(attempt_id=-1)
    assert problem.concept_id == fixture["concept"]["slug"]
    assert graph.nodes


def test_fixture_schema_keys_frozen(fixture: dict[str, Any]) -> None:
    assert set(fixture) == TOP_LEVEL_KEYS
    assert fixture["fixture_version"] == 1
    assert fixture["pii_scrubbed"] is True
    assert ATTEMPT_REF.match(fixture["attempt_ref"])
    assert set(fixture["concept"]) == CONCEPT_KEYS
    assert set(fixture["recorded"]) == RECORDED_KEYS
    for message in fixture["messages"]:
        assert set(message) == MESSAGE_KEYS
        assert message["role"] in {"student", "apollo"}
    for opportunity in fixture["question_opportunities"]:
        assert set(opportunity) == QOPP_KEYS
        assert opportunity["state"] in _VALID_TALLY_STATES
        for entry in opportunity["evidence"]:
            assert set(entry) == EVIDENCE_KEYS
    for verdict in fixture["adjudicator_output"]["verdicts"]:
        assert set(verdict) == VERDICT_KEYS


def test_turn_indexes_are_contiguous_and_ordered(fixture: dict[str, Any]) -> None:
    indexes = [message["turn_index"] for message in fixture["messages"]]
    assert indexes == list(range(len(indexes)))


def test_recorded_basis_is_on_enum_so_nothing_is_dropped(fixture: dict[str, Any]) -> None:
    """The export's off-enum ``"replay-reconstructed"`` was normalized (README §Basis).

    An off-enum basis is silently dropped-and-logged by ``_to_coverage_verdict``,
    which would leave the replay's ``coverage["basis"]`` map empty and make the
    fixture disagree with production for a reason nobody would look for.
    """
    for verdict in fixture["adjudicator_output"]["verdicts"]:
        assert verdict["basis"] in BASIS_VALUES
        assert verdict["basis"] == ("stated" if verdict["credit"] > 0 else "absent")


def test_every_verdict_carries_the_second_reader_booleans(fixture: dict[str, Any]) -> None:
    """A level->=1 replay needs a second-reader answer for every recorded node."""
    for verdict in fixture["adjudicator_output"]["verdicts"]:
        assert verdict["contradicted"] is False
        assert verdict["prompted"] is False
        assert verdict["corrected_later"] is False


# --------------------------------------------------------------------------- #
# PII sweep                                                                    #
# --------------------------------------------------------------------------- #


def test_no_pii_markers_in_any_fixture(fixture: dict[str, Any]) -> None:
    """The mandatory regex sweep, as an executable gate (README §PII sweep)."""
    hits: list[tuple[str, str]] = []
    for text in _iter_strings(fixture):
        for label, pattern in SWEEPS:
            hits.extend((label, match.group(0)) for match in pattern.finditer(text))
    assert hits == []


def test_no_identity_or_timestamp_keys_survive(fixture: dict[str, Any]) -> None:
    assert sorted(set(_iter_keys(fixture)) & BANNED_KEYS) == []


def test_every_capitalized_token_is_reviewed(fixture: dict[str, Any]) -> None:
    """Exhaustive proper-noun allowlist — a new one means an unreviewed edit."""
    found: set[str] = set()
    for text in _iter_strings(fixture):
        found.update(CAPITALIZED_TOKEN.findall(text))
    assert sorted(found - REVIEWED_TOKENS) == []


def test_every_name_shaped_sequence_is_a_published_author_or_course_term(
    fixture: dict[str, Any],
) -> None:
    found: set[str] = set()
    for text in _iter_strings(fixture):
        found.update(NAME_SEQUENCE.findall(text))
    assert sorted(found - REVIEWED_NAME_SEQUENCES) == []


# --------------------------------------------------------------------------- #
# Per-fixture role (each one is a named regression)                            #
# --------------------------------------------------------------------------- #


def test_attempt_083_is_the_single_turn_unasked_credit_shape() -> None:
    """One student paragraph, zero questions asked, A+ — the exploit W2-A gates."""
    fx = _load("attempt_083_paragraph_dump.json")
    students = [m for m in fx["messages"] if m["role"] == "student"]
    assert len(students) == 1
    assert all(q["times_asked"] == 0 for q in fx["question_opportunities"])
    assert all(q["last_asked_turn"] is None for q in fx["question_opportunities"])
    assert fx["recorded"]["served_letter"] == "A+"


def test_attempt_086_has_zero_student_messages() -> None:
    """The I7 artifact: a graded A with an empty transcript and a live ledger.

    Kept exactly as exported — it is the P0.1 empty-attempt-guard regression.
    """
    fx = _load("attempt_086_zero_transcript.json")
    assert fx["messages"] == []
    assert fx["question_opportunities"]
    assert any(q["evidence"] for q in fx["question_opportunities"])
    assert fx["recorded"]["served_letter"] == "A"


def test_attempt_124_carries_a_conflicting_graded_node() -> None:
    """Contradiction on a graded node whose credit stays below the S2' 0.6 bar."""
    fx = _load("attempt_124_conflicting_graded.json")
    conflicting = [q for q in fx["question_opportunities"] if q["state"] == "conflicting"]
    assert [q["reference_node_id"] for q in conflicting] == ["q2_four_impairments"]
    assert fx["recorded"]["topic_credits"]["q2_four_impairments"] < 0.6


def test_attempt_167_carries_the_self_correction_quote() -> None:
    """The regression content itself — a future scrub may not silently gut it.

    The sentence must survive verbatim in BOTH the transcript and the ledger
    evidence, because S2' keys on evidence recency: the corrected claim has to
    be reachable from the ledger for ``corrected_later`` to mean anything.
    """
    fx = _load("attempt_167_self_correction.json")
    assert any(SELF_CORRECTION_QUOTE in m["content"] for m in fx["messages"])
    quotes = [e["quote"] for q in fx["question_opportunities"] for e in q["evidence"]]
    assert any(SELF_CORRECTION_QUOTE in quote for quote in quotes)
    contested = [q for q in fx["question_opportunities"] if q["state"] == "conflicting"]
    assert [q["reference_node_id"] for q in contested] == ["q19_map_roots"]
    assert fx["recorded"]["topic_credits"]["q19_map_roots"] >= 0.6
