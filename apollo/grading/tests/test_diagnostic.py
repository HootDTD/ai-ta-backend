"""WU-4C2 §6.8 — the CONSTRAINED, findings-only diagnostic (injected LLM).

Every LLM call is a deterministic INJECTED stub — no live OpenAI, no network, no
container. Pins: the request is built from findings ONLY (no raw transcript), the
happy path returns the llm narrative, the deterministic post-check catches
"claimed covered for a missing key" + "introduced a misconception not in
findings", the regenerate-once-then-template fallback (exactly two llm calls), the
llm-raises -> template (NO-FALLBACK boundary), and that the module does NOT import
``OpenAI`` at top level (CI-safe import).
"""

from __future__ import annotations

from dataclasses import fields

from apollo.grading import diagnostic as diag_mod
from apollo.grading.diagnostic import (
    ConstrainedDiagnostic,
    DiagnosticFinding,
    DiagnosticRequest,
    generate_constrained_diagnostic,
)
from apollo.grading.tests._builders import (
    audited,
    contradiction_finding,
    covered_finding,
    missing_finding,
)


class _CountingLLM:
    """An llm stub returning ``returns[i]`` on call i (last value repeats);
    records every request on ``.requests`` and the call count on ``.calls``."""

    def __init__(self, *returns: str) -> None:
        self._returns = returns
        self.calls = 0
        self.requests: list[DiagnosticRequest] = []

    def __call__(self, request: DiagnosticRequest) -> str:
        idx = min(self.calls, len(self._returns) - 1)
        self.calls += 1
        self.requests.append(request)
        return self._returns[idx]


def _counting_llm(*returns: str) -> _CountingLLM:
    return _CountingLLM(*returns)


class _RaisingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: DiagnosticRequest) -> str:
        self.calls += 1
        raise RuntimeError("llm boom")


def _raising_llm() -> _RaisingLLM:
    return _RaisingLLM()


def test_request_built_from_findings_only():
    captured = {}

    def _llm(request: DiagnosticRequest) -> str:
        captured["req"] = request
        return "You taught c1 well and Apollo still needs m1."

    aud = audited(
        (
            covered_finding("c1"),
            missing_finding("m1"),
            contradiction_finding("misc.bad", student_node_ids=("n1",)),
        )
    )
    generate_constrained_diagnostic(aud, llm=_llm)
    req = captured["req"]
    assert isinstance(req, DiagnosticRequest)
    assert "c1" in req.covered_keys
    assert "m1" in req.missing_keys
    assert "misc.bad" in req.misconception_keys
    # The findings-only structural guarantee: NO raw transcript field exists on
    # the request type.
    field_names = {f.name for f in fields(DiagnosticRequest)}
    assert "transcript" not in field_names
    assert all(isinstance(f, DiagnosticFinding) for f in req.findings)


def test_happy_path_returns_llm_narrative():
    narrative = "You explained c1 clearly; Apollo still has not been shown m1."
    llm = _counting_llm(narrative)
    aud = audited((covered_finding("c1"), missing_finding("m1")))
    out = generate_constrained_diagnostic(aud, llm=llm)
    assert isinstance(out, ConstrainedDiagnostic)
    assert out.narrative == narrative
    assert out.used_fallback is False
    assert out.regenerated is False
    assert out.post_check_failures == ()
    assert llm.calls == 1


def test_post_check_flags_claimed_covered_for_missing():
    # findings mark m1 MISSING; the llm wrongly asserts "you covered m1".
    bad = "Great work — you covered m1 thoroughly."
    good = "Apollo still needs m1; you have not shown it yet."
    llm = _counting_llm(bad, good)
    aud = audited((missing_finding("m1"),))
    out = generate_constrained_diagnostic(aud, llm=llm)
    # first call failed post-check -> regenerated once -> second narrative used.
    assert out.regenerated is True
    assert out.used_fallback is False
    assert out.narrative == good
    assert llm.calls == 2


def test_regenerate_once_then_template_on_repeated_failure():
    bad = "You covered m1 perfectly."  # claims covered for a missing key, BOTH times
    llm = _counting_llm(bad, bad)
    aud = audited((missing_finding("m1"),))
    out = generate_constrained_diagnostic(aud, llm=llm)
    assert out.used_fallback is True
    assert out.regenerated is True
    assert out.narrative == diag_mod._template_narrative(llm.requests[-1])
    # exactly TWO llm calls (regenerate once, then template — never a third call)
    assert llm.calls == 2


def test_template_lists_covered_missing_misconceptions():
    aud = audited(
        (
            covered_finding("c1"),
            missing_finding("m1"),
            contradiction_finding("misc.bad", student_node_ids=("n1",)),
        )
    )
    # force the template by failing post-check both times.
    llm = _counting_llm("you covered m1", "you covered m1")
    out = generate_constrained_diagnostic(aud, llm=llm)
    assert out.used_fallback is True
    assert "c1" in out.narrative
    assert "m1" in out.narrative
    assert "misc.bad" in out.narrative


def test_diagnostic_llm_raises_falls_back_to_template():
    llm = _raising_llm()
    aud = audited((covered_finding("c1"), missing_finding("m1")))
    out = generate_constrained_diagnostic(aud, llm=llm)
    assert out.used_fallback is True
    # no exception escaped; narrative is the deterministic template
    assert "c1" in out.narrative
    assert "m1" in out.narrative


def test_introduced_misconception_not_in_findings_fails_post_check():
    # narrative names a misconception token NOT in misconception_keys.
    bad = "Apollo now believes the misc.ghost idea, which is wrong."
    good = "You taught c1 clearly; nothing else to flag."
    llm = _counting_llm(bad, good)
    aud = audited(
        (
            covered_finding("c1"),
            contradiction_finding("misc.real", student_node_ids=("n1",)),
        )
    )
    out = generate_constrained_diagnostic(aud, llm=llm)
    assert out.regenerated is True
    assert out.narrative == good


def test_no_live_openai_import():
    """Importing the module must NOT pull ``OpenAI`` at top level (CI-safe). The
    live default lazily imports it inside ``main_chat_diagnostic_llm``."""
    import ast
    import pathlib

    src = pathlib.Path(diag_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_level_imports: set[str] = set()
    for node in tree.body:  # only TOP-LEVEL statements
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                top_level_imports.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.add(alias.name)
    assert "OpenAI" not in top_level_imports
    assert not any("openai" in name.lower() for name in top_level_imports)


def test_evidence_span_absence_is_soft_not_hard():
    # A grounded narrative with no span reference is NOT a hard failure.
    aud = audited(
        (
            covered_finding("c1"),  # covered_finding sets no evidence_spans
            missing_finding("m1"),
        )
    )
    llm = _counting_llm("You explained c1; Apollo still needs m1.")
    out = generate_constrained_diagnostic(aud, llm=llm)
    assert out.used_fallback is False
    assert out.regenerated is False


def test_main_chat_diagnostic_llm_is_callable():
    """The live default is importable + callable (its body lazily imports OpenAI;
    we do not invoke a real call here)."""
    assert callable(diag_mod.main_chat_diagnostic_llm)


def test_default_llm_used_when_none(monkeypatch):
    """When ``llm`` is None the live default is resolved — patch it to a stub so
    no live OpenAI fires."""
    stub = _counting_llm("You taught c1 clearly; Apollo still needs m1.")
    monkeypatch.setattr(diag_mod, "main_chat_diagnostic_llm", stub)
    aud = audited((covered_finding("c1"), missing_finding("m1")))
    out = generate_constrained_diagnostic(aud)
    assert out.used_fallback is False
    assert stub.calls == 1
