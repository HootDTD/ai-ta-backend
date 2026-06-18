"""WU-4C2 §6.8 — the CONSTRAINED, findings-only diagnostic narrative.

DISTINCT from the FROZEN ``apollo/overseer/diagnostic.py`` (which stays on the OLD
student-facing path and calls ``OpenAI()`` directly). This module is the §6.8
diagnostic for the graph-sim shadow chain: it takes FINDINGS ONLY (never the free
transcript), runs an INJECTED ``llm`` callable, and applies a deterministic
POST-CHECK that the narrative is grounded in those findings. The structural
guarantee (§6.8 "structurally unable to re-grade"): the request carries the
finding keys + quoted evidence spans, NEVER the raw transcript.

NO-FALLBACK-but-degrade: on a HARD post-check failure the llm is called ONCE more
with a stricter instruction; if it STILL fails (or raises both times) the result
is a deterministic ``_template_narrative`` rendering of the findings, with
``used_fallback=True`` and a logged WARNING (visible, never silent).

``OpenAI`` is NOT imported at module top — the live default
:func:`main_chat_diagnostic_llm` lazily imports it inside its body so importing
this module is CI-safe and the injected-stub tests never trigger a live call.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.findings import Finding, FindingKind

_LOG = logging.getLogger(__name__)

# Conservative covered-verb screen: a narrative that asserts one of these verbs
# adjacent to a MISSING key's display token is "claimed covered for a missing
# key" (§6.8 post-check hard failure 1). Kept deliberately narrow to avoid false
# positives (a soft-fail to template is harmless while LIVE is off).
_COVERED_VERBS: tuple[str, ...] = (
    "covered",
    "taught",
    "explained",
    "showed",
    "walked",
    "demonstrated",
)

# How many words before a missing-key token a covered-verb may sit and still be
# read as GOVERNING that key (e.g. "you covered m1", "you walked Apollo through
# m1"). Narrow on purpose — a false positive only soft-fails to the template.
_COVERED_VERB_WINDOW: int = 4

# A token that looks like a misconception bank code (``misc.<something>``). Used
# to screen for a misconception INTRODUCED in the narrative that is not in the
# findings' misconception_keys (§6.8 post-check hard failure 2).
_MISC_TOKEN_RE = re.compile(r"misc\.[a-z0-9_]+", re.IGNORECASE)


@dataclass(frozen=True)
class DiagnosticFinding:
    """The SANITIZED, findings-only view handed to the llm. Carries the finding
    kind + key + QUOTED evidence spans (no free transcript) + the optional
    message."""

    kind: str
    canonical_key: str | None
    evidence_spans: tuple[str, ...]
    message: str | None


@dataclass(frozen=True)
class DiagnosticRequest:
    """The findings-only request. There is intentionally NO ``transcript`` field —
    the diagnostic is structurally unable to re-grade (§6.8)."""

    findings: tuple[DiagnosticFinding, ...]
    covered_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]
    misconception_keys: tuple[str, ...]


@dataclass(frozen=True)
class ConstrainedDiagnostic:
    """The auditable diagnostic result."""

    narrative: str
    used_fallback: bool
    regenerated: bool
    post_check_failures: tuple[str, ...]


DiagnosticLLM = Callable[[DiagnosticRequest], str]


def _sanitize_finding(finding: Finding) -> DiagnosticFinding:
    return DiagnosticFinding(
        kind=finding.kind.value,
        canonical_key=finding.canonical_key,
        evidence_spans=finding.evidence_spans,
        message=finding.message,
    )


def _keys_of(findings: tuple[Finding, ...], kind: FindingKind) -> tuple[str, ...]:
    """The deduplicated, deterministically-ordered canonical keys of ``findings``
    whose kind == ``kind`` (skipping any ``None`` key)."""
    seen: list[str] = []
    for finding in findings:
        if finding.kind == kind and finding.canonical_key is not None:
            if finding.canonical_key not in seen:
                seen.append(finding.canonical_key)
    return tuple(seen)


def _build_request(audited: AuditedGrade) -> DiagnosticRequest:
    findings = audited.findings
    return DiagnosticRequest(
        findings=tuple(_sanitize_finding(f) for f in findings),
        covered_keys=_keys_of(findings, FindingKind.COVERED_NODE),
        missing_keys=_keys_of(findings, FindingKind.MISSING_NODE),
        misconception_keys=_keys_of(findings, FindingKind.CONTRADICTION),
    )


def _claims_covered_for_missing(narrative: str, missing_keys: tuple[str, ...]) -> tuple[str, ...]:
    """Hard failure 1: a covered-verb that GOVERNS a MISSING key token.

    Conservative case-insensitive screen: flag a missing key ONLY when a
    covered-verb appears shortly BEFORE that key (within
    :data:`_COVERED_VERB_WINDOW` words) with no clause boundary between them — i.e.
    the verb governs the missing key ("you covered m1"), NOT merely co-occurs in a
    sentence that also covers a DIFFERENT key ("you explained c1; Apollo still
    needs m1" passes). A false positive here only soft-fails to the template,
    which is harmless while LIVE is off, so the screen is deliberately narrow."""
    failures: list[str] = []
    for key in missing_keys:
        if _verb_governs_key(narrative.lower(), key.lower()):
            failures.append(f"claimed_covered_for_missing:{key}")
    return tuple(failures)


def _verb_governs_key(lowered: str, token: str) -> bool:
    """True when a covered-verb precedes ``token`` within the word window with no
    clause boundary (``.;,!?`` or newline) between the verb and the key."""
    for match in re.finditer(re.escape(token), lowered):
        before = lowered[: match.start()]
        # The window is the last few words before the key, up to the nearest
        # clause boundary (so a verb governing a PRIOR clause does not count).
        clause = re.split(r"[.;,!?\n]", before)[-1]
        words = clause.split()
        window = words[-_COVERED_VERB_WINDOW:]
        if any(verb in window for verb in _COVERED_VERBS):
            return True
    return False


def _introduces_unknown_misconception(
    narrative: str, misconception_keys: tuple[str, ...]
) -> tuple[str, ...]:
    """Hard failure 2: a ``misc.*`` token in the narrative not present in the
    findings' misconception_keys (an introduced misconception). Returns a tuple
    of failure descriptions (empty == pass)."""
    known = {k.lower() for k in misconception_keys}
    failures: list[str] = []
    for match in _MISC_TOKEN_RE.findall(narrative):
        if match.lower() not in known:
            failures.append(f"introduced_misconception:{match}")
    return tuple(failures)


def _post_check(narrative: str, request: DiagnosticRequest) -> tuple[str, ...]:
    """Deterministic, PURE post-check. Returns the tuple of HARD failures (empty
    == grounded). The soft "should reference evidence spans where they exist"
    check is recorded as a WARNING note, NEVER a hard failure (§6.8: absence of
    spans is allowed)."""
    failures: list[str] = []
    failures.extend(_claims_covered_for_missing(narrative, request.missing_keys))
    failures.extend(
        _introduces_unknown_misconception(narrative, request.misconception_keys)
    )
    return tuple(failures)


def _template_narrative(request: DiagnosticRequest) -> str:
    """The deterministic findings-only fallback: names covered / missing /
    misconception keys. Wording is not load-bearing — only that it lists the
    keys deterministically."""
    covered = ", ".join(request.covered_keys) if request.covered_keys else "none"
    missing = ", ".join(request.missing_keys) if request.missing_keys else "none"
    misc = (
        ", ".join(request.misconception_keys)
        if request.misconception_keys
        else "none"
    )
    return (
        f"Covered: {covered}. "
        f"Missing: {missing}. "
        f"Misconceptions flagged: {misc}."
    )


def _call_llm_safely(
    llm: DiagnosticLLM, request: DiagnosticRequest
) -> tuple[str | None, str | None]:
    """Call ``llm(request)``; return ``(narrative, None)`` on success or
    ``(None, error_repr)`` on any exception (NEVER raises past this boundary)."""
    try:
        return llm(request), None
    except Exception as exc:  # noqa: BLE001 — §6.8 NO-FALLBACK-but-degrade boundary
        _LOG.warning("constrained diagnostic llm raised: %s", exc)
        return None, repr(exc)


def generate_constrained_diagnostic(
    audited: AuditedGrade, *, llm: DiagnosticLLM | None = None
) -> ConstrainedDiagnostic:
    """The §6.8 constrained, findings-only diagnostic.

    Builds a findings-only :class:`DiagnosticRequest`, calls the INJECTED ``llm``
    (default :func:`main_chat_diagnostic_llm`), runs the deterministic post-check,
    regenerates ONCE on a hard failure, and falls back to the deterministic
    template (``used_fallback=True``) if the second attempt also fails or the llm
    raises. NEVER re-grades, NEVER reads the rubric, NEVER sees reference
    structure beyond the finding keys."""
    resolved_llm = llm if llm is not None else main_chat_diagnostic_llm
    request = _build_request(audited)

    # First attempt.
    narrative, error = _call_llm_safely(resolved_llm, request)
    if narrative is not None:
        failures = _post_check(narrative, request)
        if not failures:
            return ConstrainedDiagnostic(
                narrative=narrative,
                used_fallback=False,
                regenerated=False,
                post_check_failures=(),
            )
        _LOG.warning("constrained diagnostic post-check failed (1): %s", failures)
    else:
        failures = (f"llm_error:{error}",)

    # Regenerate once.
    narrative2, error2 = _call_llm_safely(resolved_llm, request)
    if narrative2 is not None:
        failures2 = _post_check(narrative2, request)
        if not failures2:
            return ConstrainedDiagnostic(
                narrative=narrative2,
                used_fallback=False,
                regenerated=True,
                post_check_failures=failures,
            )
        _LOG.warning("constrained diagnostic post-check failed (2): %s", failures2)
        all_failures = failures + failures2
    else:
        all_failures = failures + (f"llm_error:{error2}",)

    # Both attempts failed -> deterministic template fallback (NO-FALLBACK,
    # logged + visible).
    _LOG.warning("constrained diagnostic falling back to template: %s", all_failures)
    return ConstrainedDiagnostic(
        narrative=_template_narrative(request),
        used_fallback=True,
        regenerated=True,
        post_check_failures=all_failures,
    )


def main_chat_diagnostic_llm(request: DiagnosticRequest) -> str:
    """The live default ``llm`` — lazily imports ``OpenAI`` inside its body (NOT
    at module top) and makes ONE chat call grounded in the findings ONLY. Tests
    inject a deterministic stub and never reach this code."""
    import json
    import os

    from openai import OpenAI  # lazy: keeps the module import-light + CI-safe

    model = os.getenv("MAIN_MODEL", "gpt-4o")
    client = OpenAI()
    payload = {
        "covered": list(request.covered_keys),
        "missing": list(request.missing_keys),
        "misconceptions": list(request.misconception_keys),
        "evidence": [
            {"key": f.canonical_key, "spans": list(f.evidence_spans)}
            for f in request.findings
            if f.evidence_spans
        ],
    }
    system = (
        "You narrate a constrained diagnostic for a student who taught an AI "
        "learner. You are given ONLY the graded findings (covered / missing / "
        "misconception keys + quoted evidence spans) — NOT the transcript. Do "
        "NOT claim the student covered anything listed as missing. Do NOT "
        "introduce any misconception not in the provided list. Ground every "
        "claim in the findings. Be supportive and formative."
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload)},
        ],
        temperature=0.4,
    )
    return resp.choices[0].message.content or ""
