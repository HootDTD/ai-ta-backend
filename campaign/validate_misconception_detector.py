"""Validation harness for the T13 misconception detector (T9-T13 chain) over
the 20 already-graded, EXISTING attempts recorded in
``campaign/out/v2-qa-2026-07-08/replay-composite/{batch-1..4,smoke}/attempts.jsonl``.

This is NOT a re-run of ``campaign/replay.py`` (that harness drives the
graph-sim shadow chain, ``build_graph_artifact``, which never calls the new
detector at all — see that module's own docstring / apollo/handlers/
artifact_writer.py:138-139). Instead this script reconstructs, per attempt_id,
exactly the inputs ``apollo/handlers/done.py::handle_done`` assembles for the
detector when ``APOLLO_MISCONCEPTION_DETECTOR`` is on, then calls the real
chain directly:

    detect_misconceptions -> gate_findings -> merge_detections -> apply_penalty

against the SAME already-recorded coverage/rubric composite that produced the
frozen (flag-OFF, pre-T13) ``scorecard.score_0_100`` / ``diagnostic_report``
in Postgres. This gives a true A/B on identical inputs: only the detector's
penalty/ceiling differs between "before" and "after".

Reconstruction, per attempt_id:
  (a) ProblemAttempt / ApolloSession rows -- db.get(...).
  (b) student_graph -- KGStore(db, neo).read_graph(attempt_id=...) (the SAME
      frozen-at-Done subgraph read.py/done.py reads).
  (c) reference_graph -- list_problems_for_concept(db, concept_id=sess.concept_id)
      matched on p.id == attempt.problem_id (done.py's own _find_problem,
      keyed on the attempt's durable problem_id rather than
      sess.current_problem_id, since these attempts may be superseded),
      then problem.to_kg_graph(attempt_id=attempt.id).
  (d) problem_text off that same Problem object.
  (e) student_utterances -- Message.content where attempt_id=X, role='student',
      ordered by turn_index (done.py's _student_utterances, copied verbatim).

Baseline (flag-OFF) composite is read directly off the recorded
``attempt.diagnostic_report['rubric']['overall']['score']`` (== the frozen
``scorecard.score_0_100``) -- no second live grading run is needed for the
"before" half, since the flag only gates whether apply_penalty ever touches
that composite; the pure functions this script calls are exercised
unconditionally regardless of the env flag's value in this process.

Run:
    APOLLO_MISCONCEPTION_DETECTOR=1 python -m campaign.validate_misconception_detector

Honesty contract: if the OpenAI judge is unavailable, this script falls back
to a stub judge (deterministic_only mode: sympy_veto + bank_pattern + gate/
merge/apply, judge tier stubbed to all-`clear`) and prints that fact loudly in
its summary -- it never fabricates judge output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("validate_misconception_detector")

ATTEMPT_IDS: tuple[int, ...] = (
    75, 77, 81, 88, 89, 95, 97, 100, 102, 105,
    106, 108, 109, 110, 111, 112, 113, 114, 115, 116,
)

REPLAY_DIRS = (
    "campaign/out/v2-qa-2026-07-08/replay-composite/batch-1",
    "campaign/out/v2-qa-2026-07-08/replay-composite/batch-2",
    "campaign/out/v2-qa-2026-07-08/replay-composite/batch-3",
    "campaign/out/v2-qa-2026-07-08/replay-composite/batch-4",
    "campaign/out/v2-qa-2026-07-08/replay-composite/smoke",
)


def _load_env_campaign() -> None:
    """Load ``.env.campaign`` (repo root) into os.environ without clobbering
    anything already set by the caller's shell (so an explicit
    APOLLO_MISCONCEPTION_DETECTOR=1 on the invoking command line always wins)."""
    env_path = Path(__file__).resolve().parent.parent / ".env.campaign"
    if not env_path.exists():
        _LOG.warning("no .env.campaign found at %s -- relying on already-exported env", env_path)
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def _load_expected_records() -> dict[int, dict[str, Any]]:
    """Read every attempts.jsonl under REPLAY_DIRS and keep the FIRST record
    seen per attempt_id in ATTEMPT_IDS (attempt 77 appears in both batch-1 and
    smoke with identical content; first-seen is deterministic and harmless)."""
    root = Path(__file__).resolve().parent.parent
    wanted = set(ATTEMPT_IDS)
    out: dict[int, dict[str, Any]] = {}
    for rel in REPLAY_DIRS:
        fp = root / rel / "attempts.jsonl"
        if not fp.exists():
            continue
        with fp.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                aid = record.get("attempt_id")
                if aid in wanted and aid not in out:
                    out[aid] = record
    missing = wanted - set(out.keys())
    if missing:
        raise RuntimeError(f"attempts.jsonl coverage missing ids: {sorted(missing)}")
    return out


def _expected_class(record: dict[str, Any]) -> str:
    """Best-effort expected class label: prefer expected_band (a real ground-truth
    band string 'strong'/'misconception'/'partial'), else fall back to the raw
    persona name (covers rows whose expected_band is None, e.g. all the
    misconception__* rows in this sample, where persona itself already encodes
    the class)."""
    band = record.get("expected_band")
    if band:
        return str(band)
    return str(record.get("persona", "unknown"))


async def _build_stub_judge_if_needed() -> tuple[Any, bool]:
    """Returns (judge_fn, used_real_judge). Prefers the real OpenAI judge;
    falls back to an all-clear stub (deterministic_only mode) ONLY if the
    OpenAI key is genuinely missing -- never silently fabricates judge output."""
    from apollo.overseer.misconception_detector.judge import make_openai_judge
    from apollo.overseer.misconception_detector.types import JudgeRaw

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        return make_openai_judge(), True

    def _stub(*, system: str, user: str) -> JudgeRaw:  # noqa: ARG001
        return JudgeRaw(content="{}", verdict_token_prob=None)

    return _stub, False


async def _run() -> dict[str, Any]:
    _load_env_campaign()

    # Imports deferred until after env is loaded (some modules read env at
    # import time, e.g. misconception_detector/config.py's calibration knobs).
    from sqlalchemy import select

    from apollo.overseer.misconception_bank import load_for_concept
    from apollo.overseer.misconception_detector.apply import apply_penalty
    from apollo.overseer.misconception_detector.centrality import compute_centrality
    from apollo.overseer.misconception_detector.config import (
        detector_enabled,
        struct_cokey_enabled,
        trace_enabled,
    )
    from apollo.overseer.misconception_detector.detector import detect_misconceptions
    from apollo.overseer.misconception_detector.gate import gate_findings
    from apollo.overseer.misconception_detector.merge import merge_detections
    from apollo.overseer.misconception_detector.opposes_index import build_opposes_index
    from apollo.overseer.misconception_detector.trace import trace_attempt
    from apollo.persistence.models import ApolloSession, Message, ProblemAttempt
    from apollo.persistence.neo4j_client import Neo4jClient
    from apollo.knowledge_graph.store import KGStore
    from apollo.overseer.problem_selector import list_problems_for_concept
    from apollo.projections.scorecard import load_bands, _band_for
    from database.session import get_db_session
    from indexing.document_embedder import embed_texts

    if not detector_enabled():
        _LOG.warning(
            "APOLLO_MISCONCEPTION_DETECTOR is not truthy in this process env -- "
            "the harness calls detect_misconceptions/gate/merge/apply directly "
            "regardless (the flag only gates done.py's own live wiring), so "
            "results are unaffected, but noting this for the record."
        )

    judge_fn, used_real_judge = await _build_stub_judge_if_needed()
    mode = "full_judge" if used_real_judge else "deterministic_only"

    def _embed_fn(text: str) -> list[float]:
        vectors = embed_texts([text])
        return vectors[0] if vectors else []

    expected_records = _load_expected_records()
    bands = load_bands()

    neo = Neo4jClient.from_env()
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        async for db in get_db_session():
            for attempt_id in ATTEMPT_IDS:
                record = expected_records[attempt_id]
                try:
                    attempt = await db.get(ProblemAttempt, attempt_id)
                    if attempt is None:
                        raise RuntimeError(f"attempt {attempt_id} not found in local stack")
                    sess = await db.get(ApolloSession, int(attempt.session_id))
                    if sess is None:
                        raise RuntimeError(f"session for attempt {attempt_id} not found")

                    store = KGStore(db, neo)
                    student_graph = await store.read_graph(attempt_id=int(attempt.id))

                    problems = await list_problems_for_concept(db, concept_id=sess.concept_id)
                    problem = None
                    for p in problems:
                        if p.id == str(attempt.problem_id):
                            problem = p
                            break
                    if problem is None:
                        raise RuntimeError(
                            f"problem {attempt.problem_id!r} not in bank for "
                            f"concept {sess.concept_id!r} (attempt {attempt_id})"
                        )
                    reference_graph = problem.to_kg_graph(attempt_id=int(attempt.id))

                    student_utterances = tuple(
                        (
                            await db.execute(
                                select(Message.content)
                                .where(Message.attempt_id == attempt_id)
                                .where(Message.role == "student")
                                .order_by(Message.turn_index)
                            )
                        )
                        .scalars()
                        .all()
                    )

                    report = attempt.diagnostic_report or {}
                    rubric = report.get("rubric") or {}
                    overall = rubric.get("overall") or {}
                    baseline_score = overall.get("score")
                    if baseline_score is None:
                        # Fall back to the recorded scorecard's score_0_100 --
                        # same number, different read path (both trace to the
                        # SAME flag-OFF rubric['overall']['score']).
                        baseline_score = record.get("scorecard", {}).get("score_0_100")
                    if baseline_score is None:
                        raise RuntimeError(
                            f"attempt {attempt_id} has no recorded baseline score "
                            "(diagnostic_report.rubric.overall.score and "
                            "scorecard.score_0_100 both missing)"
                        )
                    composite_before = round(float(baseline_score) / 100.0, 6)
                    baseline_band = _band_for(composite_before, bands)

                    detection = await detect_misconceptions(
                        db,
                        attempt_id=int(attempt.id),
                        concept_id=sess.concept_id,
                        student_graph=student_graph,
                        reference_graph=reference_graph,
                        problem_text=problem.problem_text,
                        student_utterances=student_utterances,
                        judge_fn=judge_fn,
                        embed_fn=_embed_fn,
                    )
                    # F-struct (structural co-key) — DEFAULT OFF sub-flag,
                    # gated exactly like done.py: when OFF, `opposes_index`
                    # stays `{}` and gate/trace see the same empty map they
                    # always defaulted to (byte-identical to pre-F-struct).
                    # When ON, resolve the concept bank's `opposes` links
                    # (each an `entity_key`) against the reference graph so a
                    # judge-localized-but-unnamed error can dock structurally.
                    opposes_index: dict[str, str] = {}
                    if struct_cokey_enabled():
                        bank_entries = tuple(
                            await load_for_concept(db, concept_id=sess.concept_id)
                        )
                        opposes_index = build_opposes_index(
                            reference_graph, bank_entries
                        )
                    gated = gate_findings(
                        detection.per_concept, opposes_index=opposes_index
                    )
                    centrality = compute_centrality(reference_graph)
                    outcome = merge_detections(gated, centrality=centrality)
                    composite_after = apply_penalty(composite=composite_before, outcome=outcome)
                    detector_band = _band_for(composite_after, bands)

                    misconceptions_found = sorted(
                        m["canonical_key"] for m in outcome.misconceptions
                    )

                    expected_misc = sorted((record.get("expected") or {}).get("misconceptions", []))
                    expected_cls = _expected_class(record)
                    is_control = expected_cls in ("strong", "partial")

                    # Phase-1 diagnostic trace (default OFF, APOLLO_MISC_TRACE):
                    # emit one JSONL row per reference-graph node revealing, per
                    # node, exactly what the judge said and which gate row fired
                    # — misconception attempts AND controls, traced identically.
                    # Uses the SCORECARD band (`detector_band`) + the labeled
                    # `is_control`, so `is_false_strong` is the real residual
                    # false-Strong roll-up the recall gap targets.
                    if trace_enabled():
                        trace_attempt(
                            attempt_id=attempt_id,
                            reference_graph=reference_graph,
                            detection=detection,
                            gated=gated,
                            outcome=outcome,
                            centrality=centrality,
                            final_band=detector_band,
                            is_control=is_control,
                            opposes_index=opposes_index,
                        )

                    control_credit_ok = (
                        True
                        if not is_control
                        else (outcome.misconception_penalty == 0.0 and not misconceptions_found)
                    )

                    rows.append(
                        {
                            "attempt_id": attempt_id,
                            "persona": record.get("persona"),
                            "expected_class": expected_cls,
                            "is_control": is_control,
                            "baseline_band": baseline_band,
                            "baseline_composite": composite_before,
                            "detector_band": detector_band,
                            "detector_composite": composite_after,
                            "penalty": outcome.misconception_penalty,
                            "ceiling_applied": outcome.ceiling_applied,
                            "misconceptions_found": misconceptions_found,
                            "expected_misconceptions": expected_misc,
                            "control_credit_ok": control_credit_ok,
                            "n_findings_raw": len(detection.per_concept),
                            "n_findings_gated": len(gated),
                        }
                    )
                    _LOG.info(
                        "attempt=%s expected=%s baseline=%s(%.4f) detector=%s(%.4f) "
                        "penalty=%.4f misc=%s",
                        attempt_id, expected_cls, baseline_band, composite_before,
                        detector_band, composite_after, outcome.misconception_penalty,
                        misconceptions_found,
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOG.exception("attempt %s failed", attempt_id)
                    errors.append({"attempt_id": attempt_id, "error": f"{type(exc).__name__}: {exc}"})
            break
    finally:
        await neo.close()

    return {
        "mode": mode,
        "used_real_judge": used_real_judge,
        "rows": rows,
        "errors": errors,
    }


def _summarize(result: dict[str, Any]) -> str:
    rows = result["rows"]
    lines: list[str] = []
    lines.append(f"mode={result['mode']} used_real_judge={result['used_real_judge']}")
    lines.append(f"attempts attempted={len(rows) + len(result['errors'])} ok={len(rows)} errors={len(result['errors'])}")

    misconception_rows = [r for r in rows if not r["is_control"]]
    control_rows = [r for r in rows if r["is_control"]]

    n_misc_with_finding = sum(1 for r in misconception_rows if r["misconceptions_found"])
    n_baseline_false_strong = sum(1 for r in misconception_rows if r["baseline_band"] == "Strong")
    n_after_false_strong = sum(1 for r in misconception_rows if r["detector_band"] == "Strong")
    n_control_leak = sum(1 for r in control_rows if not r["control_credit_ok"])

    lines.append(
        f"misconception-class attempts: {len(misconception_rows)}; "
        f">=1 misconception detected: {n_misc_with_finding}"
    )
    lines.append(
        f"false-Strong on misconception-class: baseline={n_baseline_false_strong} "
        f"-> after-penalty={n_after_false_strong}"
    )
    lines.append(
        f"strong/partial CONTROL attempts: {len(control_rows)}; "
        f"false positives (penalty/misconception on a control): {n_control_leak}"
    )
    if result["errors"]:
        lines.append(f"errors: {result['errors']}")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = asyncio.run(_run())
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print("\n=== SUMMARY ===")
    print(_summarize(result))


if __name__ == "__main__":
    main()
