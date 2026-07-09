from apollo.agent.leakage_judge import JudgeVerdict
from apollo.clarification.leak_guard import guard_clarification_reply


def _concept():
    return object()  # the injected stub judges ignore `concept`; no real ConceptDefinition needed


def test_confident_leak_redrafts_without_probes():
    def judge(*, draft, concept, history, kg_summary):
        return JudgeVerdict(leaks=True, offending_phrase="lower", reason="x", confidence=0.9)

    out = guard_clarification_reply(
        draft="...the pressure is lower...",
        concept=_concept(),
        history=[],
        kg_summary="k",
        regenerate_without_probes=lambda: "SAFE REPLY",
        judge=judge,
    )
    assert out == "SAFE REPLY"


def test_low_confidence_leak_is_kept():
    def judge(**kw):
        return JudgeVerdict(leaks=True, offending_phrase="maybe", reason="x", confidence=0.3)

    out = guard_clarification_reply(
        draft="kept probe",
        concept=_concept(),
        history=[],
        kg_summary="k",
        regenerate_without_probes=lambda: "UNUSED",
        judge=judge,
    )
    assert out == "kept probe"  # below CONFIDENCE_THRESHOLD (0.6)


def test_clean_reply_is_kept():
    def judge(**kw):
        return JudgeVerdict(leaks=False, offending_phrase=None, reason=None, confidence=1.0)

    out = guard_clarification_reply(
        draft="clean probe",
        concept=_concept(),
        history=[],
        kg_summary="k",
        regenerate_without_probes=lambda: "UNUSED",
        judge=judge,
    )
    assert out == "clean probe"


def test_judge_error_soft_fail_open():
    def judge(**kw):
        raise RuntimeError("503")

    out = guard_clarification_reply(
        draft="original",
        concept=_concept(),
        history=[],
        kg_summary="k",
        regenerate_without_probes=lambda: "REGEN",
        judge=judge,
    )
    assert out == "original"  # spec §12: soft fail open, never block teaching
