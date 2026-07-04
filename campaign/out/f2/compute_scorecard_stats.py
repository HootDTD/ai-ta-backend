"""F2 scorecard statistics — pure read of campaign/out/f2/attempts.jsonl.

Computes, per persona archetype and overall:
  - attempt counts (ok / error), graph-abstention rate + reason histogram,
  - credit stats (credited vs expected credited),
  - misconception detection (asserted misconceptions vs expected),
  - control_credit_leak count (same rule as campaign.replay: a control
    persona whose LIVE graph ledger credits any key beyond its expected
    credited set), computed on the RECORDED artifacts, not a replay,
  - O1 latency: p50/p95 of grading_latency_ms (canonical artifact) and of
    total attempt wall-clock,
  - clarification-trace presence.

Usage: python campaign/out/f2/compute_scorecard_stats.py
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
CONTROL_PERSONAS = {"misconception", "vague_then_clarifies"}


def load_attempts() -> list[dict]:
    lines = (OUT_DIR / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def graph_payload(att: dict) -> dict | None:
    for slot in ("artifact_pair", "artifact_canonical"):
        payload = att.get(slot)
        if isinstance(payload, dict) and payload.get("grader_used") == "graph":
            return payload
    return None


def ledger_keys(payload: dict, status: str) -> set[str]:
    return {
        e.get("key") or e.get("canonical_key")
        for e in payload.get("node_ledger", [])
        if e.get("status") == status
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(q * (len(vs) - 1))))
    return vs[idx]


def main() -> None:
    attempts = load_attempts()
    by_class: dict[str, list[dict]] = defaultdict(list)
    for a in attempts:
        by_class[a.get("persona", "?")].append(a)

    overall_latency_ms: list[float] = []
    overall_wall_ms: list[float] = []
    leak_attempts: list[dict] = []
    reason_hist: Counter[str] = Counter()

    print(f"total records: {len(attempts)}")
    summary: dict[str, dict] = {}

    for cls, atts in sorted(by_class.items()):
        ok = [a for a in atts if a.get("status") == "ok"]
        err = [a for a in atts if a.get("status") != "ok"]
        abstained = 0
        graded = 0
        credited_frac: list[float] = []
        misc_detected = 0
        misc_expected_n = 0
        clar_traces = 0
        for a in ok:
            gp = graph_payload(a)
            can = a.get("artifact_canonical") or {}
            lat = can.get("grading_latency_ms")
            if lat is not None:
                overall_latency_ms.append(float(lat))
            wt = (a.get("wall_times") or {}).get("total_ms")
            if wt is not None:
                overall_wall_ms.append(float(wt))
            expected = a.get("expected") or {}
            exp_credited = set(expected.get("credited", []))
            exp_misc = set(expected.get("misconceptions", []))
            misc_expected_n += len(exp_misc)
            if gp is None:
                continue
            graded += 1
            abst = gp.get("abstention")
            if abst and abst.get("reasons"):
                abstained += 1
                for r in abst["reasons"]:
                    reason_hist[str(r)] += 1
            actual_credited = ledger_keys(gp, "credited")
            if exp_credited:
                credited_frac.append(
                    len(actual_credited & exp_credited) / len(exp_credited)
                )
            asserted = {
                m.get("key") or m.get("canonical_key")
                for m in gp.get("misconceptions", [])
            }
            if asserted & exp_misc:
                misc_detected += 1
            if (gp.get("clarification_trace") or (a.get("artifact_canonical") or {}).get(
                "clarification_trace"
            )):
                clar_traces += 1
            if cls in CONTROL_PERSONAS and (actual_credited - exp_credited):
                leak_attempts.append(
                    {
                        "attempt_id": a.get("attempt_id"),
                        "persona_id": a.get("persona_id"),
                        "class": cls,
                        "leaked": sorted(actual_credited - exp_credited),
                    }
                )
        summary[cls] = {
            "n": len(atts),
            "ok": len(ok),
            "err": len(err),
            "graph_rows": graded,
            "abstained": abstained,
            "abstention_rate": round(abstained / graded, 3) if graded else None,
            "mean_expected_credit_recall": round(
                statistics.mean(credited_frac), 3
            )
            if credited_frac
            else None,
            "attempts_with_expected_misc_detected": misc_detected,
            "clarification_traces": clar_traces,
        }

    print(json.dumps(summary, indent=2))
    print("abstention reasons:", dict(reason_hist))
    print(f"control_credit_leak attempts: {len(leak_attempts)}")
    for leak_attempt in leak_attempts:
        print("  LEAK", leak_attempt)
    print(
        "grading_latency_ms p50/p95:",
        percentile(overall_latency_ms, 0.5),
        percentile(overall_latency_ms, 0.95),
        f"(n={len(overall_latency_ms)}, max={max(overall_latency_ms) if overall_latency_ms else None})",
    )
    print(
        "attempt wall total_ms p50/p95:",
        percentile(overall_wall_ms, 0.5),
        percentile(overall_wall_ms, 0.95),
    )
    (OUT_DIR / "scorecard-stats.json").write_text(
        json.dumps(
            {
                "per_class": summary,
                "abstention_reasons": dict(reason_hist),
                "control_credit_leaks": leak_attempts,
                "latency_ms": {
                    "grading_p50": percentile(overall_latency_ms, 0.5),
                    "grading_p95": percentile(overall_latency_ms, 0.95),
                    "grading_max": max(overall_latency_ms) if overall_latency_ms else None,
                    "wall_p50": percentile(overall_wall_ms, 0.5),
                    "wall_p95": percentile(overall_wall_ms, 0.95),
                    "n": len(overall_latency_ms),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote scorecard-stats.json")


if __name__ == "__main__":
    main()
