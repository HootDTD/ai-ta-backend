from apollo.overseer.misconception import MisconceptionSignal, summarize_for_rubric


def test_retained_rubric_helper_scores_resolved_and_unresolved_signals():
    signals = [
        MisconceptionSignal(fired=True, state="probe", bank_code="old"),
        MisconceptionSignal.default(),
        MisconceptionSignal(fired=True, state="probe", bank_code="current"),
    ]
    assert summarize_for_rubric(signals) == {"old": 1.0, "current": 0.5}
