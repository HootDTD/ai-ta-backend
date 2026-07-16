from apollo.overseer.grading_flags import topic_score_served_enabled, transcript_grader_enabled


def test_grading_flags_default_off(monkeypatch):
    monkeypatch.delenv("APOLLO_TOPIC_SCORE_SERVED", raising=False)
    monkeypatch.delenv("APOLLO_TRANSCRIPT_GRADER", raising=False)
    assert topic_score_served_enabled() is False
    assert transcript_grader_enabled() is False


def test_grading_flags_accept_truthy_values(monkeypatch):
    monkeypatch.setenv("APOLLO_TOPIC_SCORE_SERVED", "yes")
    monkeypatch.setenv("APOLLO_TRANSCRIPT_GRADER", "ON")
    assert topic_score_served_enabled() is True
    assert transcript_grader_enabled() is True
