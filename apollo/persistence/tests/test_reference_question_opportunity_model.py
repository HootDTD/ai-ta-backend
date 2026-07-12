from apollo.persistence.models import ReferenceQuestionOpportunity


def test_reference_question_opportunity_schema_contract():
    columns = ReferenceQuestionOpportunity.__table__.columns
    assert {
        "attempt_id",
        "session_id",
        "reference_node_id",
        "state",
        "question",
        "asked_turn",
        "answered_turn",
    }.issubset(columns.keys())
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in ReferenceQuestionOpportunity.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("attempt_id", "reference_node_id") in unique_sets
