from apollo.persistence.models import CLARIFICATION_STATES, Clarification


def test_clarification_states_allowlist_matches_sql_check():
    # Mirror of migration 032's CHECK (state IN (...)). Keep in lockstep.
    assert CLARIFICATION_STATES == ("asked_waiting", "confirmed", "refuted", "vague")


def test_clarification_table_columns():
    cols = Clarification.__table__.columns
    expected = {
        "id", "attempt_id", "session_id", "user_id", "search_space_id",
        "concept_id", "node_id", "candidate_key", "state", "probe_question",
        "original_statement", "clarification_text", "asked_turn", "answered_turn",
        "created_at", "updated_at",
    }
    assert set(cols.keys()) == expected
    assert cols["clarification_text"].nullable is True
    assert cols["answered_turn"].nullable is True
    assert cols["node_id"].nullable is False


def test_clarification_unique_attempt_node():
    uniques = {
        tuple(c.name for c in con.columns)
        for con in Clarification.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("attempt_id", "node_id") in uniques
