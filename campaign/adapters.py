"""Task D3(b) — real-artifact -> judge/report plain-dict adapters.

``campaign/judges/s3_student_fidelity.py``, ``s4_apollo_coherence.py``,
``s5_misconceptions.py`` and ``campaign/report.py`` were all built (Task E1/
E3) against HAND-DOCUMENTED plain-dict shapes, before the canonical artifact
(``apollo.grading.artifact_build``) existed on this branch. Now that Phase
A/B's real ``GradingArtifact``/``build_graph_artifact``/``build_llm_artifact``
payloads ARE importable here, this module is the single seam that reshapes a
REAL artifact dict (plus the persona's authored ``ExpectedLedger`` and the
session transcript) into the EXACT dict shapes those judges/`report.py`
consume — so the shape duplication both modules' docstrings flagged as a
"once the branches are combined" deviation is retired by this file, not by
editing the judges/report themselves (their contracts are frozen; this is
the thin adapter the E1/E3 docstrings promised).

Two real shape mismatches this module bridges (discovered while wiring this
adapter — the judges were authored against a plain sketch, the real
artifact_build.py schema differs in these two respects):

1. Ledger entries key their identity as ``canonical_key`` (see
   ``apollo/grading/artifact_build.py``), but every judge (S3's
   ``ledger_vs_expected``/``build_items``, S5's asserted-misconception items)
   reads ``entry["key"]``. :func:`_ledger_entry_for_judges` renames the field;
   nothing else about an entry's shape changes.
2. ``ExpectedLedger.to_ledger_dict()`` already produces the exact
   ``{credited, unresolved, misconceptions}`` shape S3 wants — reused
   verbatim, not reinvented here.

Pure module: no DB/Neo4j/LLM/HTTP imports. Every function takes already-loaded
plain data (artifact payload dicts, ``PersonaAttempt``/``ExpectedLedger``,
transcript turns) and returns a plain dict/list — trivially unit-testable
against fixtures built from the REAL ``artifact_build`` builders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apollo.projections.scorecard import render_scorecard
from campaign.cast.personas.schema import ExpectedLedger
from campaign.cast.subjects import AUTHORED_SUBJECTS, SEEDED_SUBJECTS, is_held_out

__all__ = [
    "subject_kind_for",
    "all_subject_kinds",
    "transcript_to_text",
    "ledger_entry_to_judge_shape",
    "node_ledger_to_judge_shape",
    "attempt_to_s3_item",
    "misconception_bank_lookup",
    "attempt_to_s5_item",
    "extract_apollo_questions",
    "clarification_trace_to_judge_shape",
    "attempt_to_s4_item",
    "graph_payload_for",
    "llm_payload_for",
    "attempt_to_report_record",
]


def subject_kind_for(subject_key: str) -> str:
    """``"seeded" | "wu_aas" | "held_out" | "unknown"`` for a campaign subject
    key, per the real ``campaign.cast.subjects`` registry — the single source
    of truth ``campaign.report.classify_subject`` is deliberately decoupled
    from (its docstring: "Task D1's, not this task's, contract to keep
    stable"). This is that promised adapter."""
    if subject_key in SEEDED_SUBJECTS:
        return "seeded"
    if subject_key in AUTHORED_SUBJECTS:
        return "held_out" if is_held_out(subject_key) else "wu_aas"
    return "unknown"


def all_subject_kinds() -> dict[str, str]:
    """``{subject_key: kind}`` for every registered subject — the exact
    ``subject_kinds`` mapping ``campaign.report.build_report``'s breadth gate
    consumes."""
    keys = [*SEEDED_SUBJECTS, *AUTHORED_SUBJECTS]
    return {key: subject_kind_for(key) for key in keys}


def transcript_to_text(transcript: Sequence[Mapping[str, Any]]) -> str:
    """Render a driver transcript (``[{role, content}, ...]``) into the flat
    ``"role: content"`` text S3/S4's system prompts expect ("you see ONLY the
    full transcript of the session")."""
    return "\n".join(f"{turn.get('role', '?')}: {turn.get('content', '')}" for turn in transcript)


def ledger_entry_to_judge_shape(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One ``node_ledger``/``misconceptions`` row -> the judge-facing shape:
    rename ``canonical_key`` -> ``key`` (see module docstring mismatch #1),
    keep every other field verbatim."""
    out = dict(entry)
    out["key"] = out.pop("canonical_key", None)
    return out


def node_ledger_to_judge_shape(node_ledger: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [ledger_entry_to_judge_shape(entry) for entry in node_ledger]


def attempt_to_s3_item(
    *,
    attempt_id: Any,
    transcript: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    expected: ExpectedLedger,
) -> dict[str, Any]:
    """One ``campaign.judges.s3_student_fidelity`` input item: the real
    artifact's node ledger (renamed to the judge's ``key`` field) + the
    persona's authored expected ledger, keyed by ``attempt_id``."""
    return {
        "attempt_id": attempt_id,
        "transcript": transcript_to_text(transcript),
        "node_ledger": node_ledger_to_judge_shape(artifact.get("node_ledger", [])),
        "expected": expected.to_ledger_dict(),
    }


def misconception_bank_lookup(subject: str, concept: str) -> dict[str, str]:
    """``{misc_key: description}`` for one subject/concept's real (or, for a
    PROVISIONAL WU-AAS subject, hand-authored) misconception bank — reuses
    the D2 validator's on-disk loader so this never hand-mints a description
    the bank doesn't actually carry."""
    from campaign.cast.personas import validate as _validate

    misc_path = _validate._concept_dir(subject, concept) / "misconceptions.json"
    if not misc_path.exists():
        return {}
    import json

    data = json.loads(misc_path.read_text(encoding="utf-8"))
    return {entry["key"]: entry.get("description", "") for entry in data.get("misconceptions", [])}


def attempt_to_s5_item(
    *,
    attempt_id: Any,
    artifact: Mapping[str, Any],
    expected: ExpectedLedger,
    subject: str,
    concept: str,
) -> dict[str, Any]:
    """One ``campaign.judges.s5_misconceptions`` input item: every misconception
    the real artifact asserted, each with its bank description looked up from
    the real (or provisional) misconception bank on disk."""
    bank = misconception_bank_lookup(subject, concept)
    asserted = [
        {
            "key": m.get("canonical_key"),
            "utterance": m.get("evidence_span"),
            "bank_description": bank.get(m.get("canonical_key"), ""),
        }
        for m in artifact.get("misconceptions", [])
    ]
    return {
        "attempt_id": attempt_id,
        "expected": expected.to_ledger_dict(),
        "asserted_misconceptions": asserted,
    }


def extract_apollo_questions(transcript: Sequence[Mapping[str, Any]]) -> list[str]:
    """Apollo's turns that read as a question (contain ``"?"``) — the S4
    "confused-learner questions" input. A driver-side heuristic (Apollo has
    no structured "this is a clarification probe" flag on the wire; see
    ``campaign/cast/student.py`` module docstring for the same caveat)."""
    return [
        str(turn.get("content", ""))
        for turn in transcript
        if turn.get("role") == "apollo" and "?" in str(turn.get("content", ""))
    ]


def clarification_trace_to_judge_shape(trace: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The artifact's clarification trace (``artifact_writer._load_clarification_trace``
    shape: ``probe_question``/``clarification_text``/``credit``) reshaped to
    the ``{question, answer, credit}`` triples S4's prompt describes."""
    return [
        {
            "question": row.get("probe_question"),
            "answer": row.get("clarification_text"),
            "credit": row.get("credit"),
        }
        for row in trace
    ]


def attempt_to_s4_item(
    *, attempt_id: Any, transcript: Sequence[Mapping[str, Any]], artifact: Mapping[str, Any]
) -> dict[str, Any]:
    """One ``campaign.judges.s4_apollo_coherence`` input item for one sampled
    session: Apollo's questions from the transcript + the real artifact's
    clarification trace + the final unresolved/misconception ledger keys."""
    node_ledger = node_ledger_to_judge_shape(artifact.get("node_ledger", []))
    unresolved_keys = [e.get("key") for e in node_ledger if e.get("status") == "unresolved"]
    misconception_keys = [e.get("key") for e in node_ledger if e.get("status") == "misconception"]
    return {
        "attempt_id": attempt_id,
        "apollo_questions": extract_apollo_questions(transcript),
        "clarification_trace": clarification_trace_to_judge_shape(
            artifact.get("clarification_trace", [])
        ),
        "unresolved_keys": unresolved_keys,
        "misconception_keys": misconception_keys,
    }


def graph_payload_for(
    *, artifact_canonical: Mapping[str, Any] | None, artifact_pair: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Whichever of the two captured rows is the graph-grader's payload
    (``grader_used == "graph"``) — canonical when the graph grade was served
    live, the paired row in shadow-mode tuning runs where the LLM is always
    served."""
    for payload in (artifact_canonical, artifact_pair):
        if payload is not None and payload.get("grader_used") == "graph":
            return dict(payload)
    return None


def llm_payload_for(
    *, artifact_canonical: Mapping[str, Any] | None, artifact_pair: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Whichever of the two captured rows is the LLM-fallback payload."""
    for payload in (artifact_canonical, artifact_pair):
        if payload is not None and payload.get("grader_used") == "llm_fallback":
            return dict(payload)
    return None


def attempt_to_report_record(
    *,
    attempt_id: Any,
    subject: str,
    artifact_canonical: Mapping[str, Any] | None,
    artifact_pair: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The exact ``campaign.report.build_report`` per-attempt dict shape
    (module docstring: ``{attempt_id, subject, band, grading_latency_ms,
    shadow_succeeded, shadow_abstained, graph_composite, llm_composite}``),
    built from the two real artifact payloads a Done-click produced.

    ``band`` is rendered from the CANONICAL payload (the grade actually
    served, matching the student-facing scorecard) via the real
    ``render_scorecard`` — never recomputed. ``shadow_succeeded``/
    ``shadow_abstained`` read the graph payload's abstention block (a graph
    payload existing at all means the shadow chain ran without raising, per
    ``apollo.handlers.artifact_writer``'s docstring); no graph payload means
    the shadow flag was off or the chain never produced a result."""
    graph_payload = graph_payload_for(
        artifact_canonical=artifact_canonical, artifact_pair=artifact_pair
    )
    llm_payload = llm_payload_for(
        artifact_canonical=artifact_canonical, artifact_pair=artifact_pair
    )
    band = render_scorecard(dict(artifact_canonical))["band"] if artifact_canonical else None
    grading_latency_ms = (
        artifact_canonical.get("grading_latency_ms") if artifact_canonical else None
    )
    shadow_succeeded = graph_payload is not None
    shadow_abstained = bool((graph_payload or {}).get("abstention", {}).get("abstained"))
    graph_composite = (
        (graph_payload.get("scores") or {}).get("composite") if graph_payload else None
    )
    llm_composite = (llm_payload.get("scores") or {}).get("composite") if llm_payload else None
    return {
        "attempt_id": attempt_id,
        "subject": subject,
        "band": band,
        "grading_latency_ms": grading_latency_ms,
        "shadow_succeeded": shadow_succeeded,
        "shadow_abstained": shadow_abstained,
        "graph_composite": graph_composite,
        "llm_composite": llm_composite,
    }
