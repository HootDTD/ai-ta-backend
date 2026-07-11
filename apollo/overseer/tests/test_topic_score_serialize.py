"""Tests for the shared ``TopicScoreResult`` JSON serializer (2026-07-10
design spec §2/§3 field names, reused by the artifact writer and the served
``student_response["topics"]`` payload)."""

from __future__ import annotations

from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult
from apollo.overseer.topic_score_serialize import serialize_topic_score, serialize_topics


def _result() -> TopicScoreResult:
    return TopicScoreResult(
        score=85,
        letter="A-",
        coverage_component=0.9,
        misconception_dock=0.05,
        topics=(
            TopicCredit(
                canonical_key="eq1",
                display_name="Bernoulli",
                credit=1.0,
                status="covered",
                weight=0.7,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.x",
                        resolved=True,
                        dock_points=0.0,
                        evidence_span="corrected span",
                    ),
                ),
            ),
            TopicCredit(
                canonical_key="_general",
                display_name=None,
                credit=0.0,
                status="missing",
                weight=0.0,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.stray",
                        resolved=False,
                        dock_points=0.05,
                        evidence_span=None,
                    ),
                ),
            ),
        ),
    )


def test_serialize_topic_score_top_level_field_names():
    block = serialize_topic_score(_result())
    assert set(block.keys()) == {
        "score",
        "letter",
        "coverage_component",
        "misconception_dock",
        "topics",
    }
    assert block["score"] == 85
    assert block["letter"] == "A-"
    assert block["coverage_component"] == 0.9
    assert block["misconception_dock"] == 0.05


def test_serialize_topic_field_names():
    block = serialize_topic_score(_result())
    topic = block["topics"][0]
    assert set(topic.keys()) == {
        "canonical_key",
        "display_name",
        "credit",
        "status",
        "weight",
        "misconceptions",
    }
    assert topic["canonical_key"] == "eq1"
    assert topic["display_name"] == "Bernoulli"
    assert topic["credit"] == 1.0
    assert topic["status"] == "covered"
    assert topic["weight"] == 0.7


def test_serialize_misconception_field_names():
    block = serialize_topic_score(_result())
    misc = block["topics"][0]["misconceptions"][0]
    assert set(misc.keys()) == {"canonical_key", "resolved", "dock_points", "evidence_span"}
    assert misc["canonical_key"] == "misc.x"
    assert misc["resolved"] is True
    assert misc["dock_points"] == 0.0
    assert misc["evidence_span"] == "corrected span"


def test_serialize_misconception_none_evidence_span_preserved():
    block = serialize_topic_score(_result())
    general = block["topics"][1]
    assert general["canonical_key"] == "_general"
    assert general["display_name"] is None
    misc = general["misconceptions"][0]
    assert misc["evidence_span"] is None


def test_serialize_topics_matches_topics_key_of_full_block():
    result = _result()
    assert serialize_topics(result) == serialize_topic_score(result)["topics"]


def test_serialize_topic_score_empty_topics():
    empty = TopicScoreResult(
        score=0,
        letter="F",
        coverage_component=0.0,
        misconception_dock=0.0,
        topics=(),
    )
    block = serialize_topic_score(empty)
    assert block["topics"] == []


def test_serialize_returns_plain_dicts_and_lists_not_dataclasses():
    block = serialize_topic_score(_result())
    assert isinstance(block, dict)
    assert isinstance(block["topics"], list)
    assert all(isinstance(t, dict) for t in block["topics"])
    assert all(isinstance(m, dict) for t in block["topics"] for m in t["misconceptions"])
