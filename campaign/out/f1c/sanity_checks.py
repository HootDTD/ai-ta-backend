"""F1c step-3 sanity checks over campaign/out/f1c/attempts.jsonl:

  1. every ok attempt has BOTH artifact rows (canonical + pair);
  2. canonical composites are non-zero and bands vary (T1 fix 3cf239f live);
  3. pair/graph abstention count + abstention-reasons histogram (T2 fix
     3e38f25 -- expect below F1b's 36/36 baseline, not necessarily zero).

Usage: python -m campaign.out.f1c.sanity_checks
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from apollo.projections.scorecard import render_scorecard  # noqa: E402
from campaign.adapters import graph_payload_for, llm_payload_for  # noqa: E402
from campaign.judges.base import load_jsonl  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent


def main() -> None:
    attempts = load_jsonl(OUT_DIR / "attempts.jsonl")
    by_subject_status: Counter = Counter()
    both_rows = 0
    missing_rows: list = []
    composites: list[float] = []
    bands: Counter = Counter()
    graph_n = 0
    abstained_n = 0
    not_abstained_n = 0
    reasons_hist: Counter = Counter()
    per_subject_graph: dict[str, list[int]] = {}
    latencies: list[float] = []

    for att in attempts:
        subj = att.get("subject", "?")
        status = att.get("status")
        by_subject_status[(subj, status)] += 1
        if status != "ok":
            continue

        canonical = att.get("artifact_canonical")
        pair = att.get("artifact_pair")
        if canonical and pair:
            both_rows += 1
        else:
            missing_rows.append(
                (att.get("persona_id"), bool(canonical), bool(pair))
            )

        if canonical:
            comp = (canonical.get("scores") or {}).get("composite")
            if comp is not None:
                composites.append(float(comp))
            try:
                bands[render_scorecard(dict(canonical)).get("band")] += 1
            except Exception as exc:  # noqa: BLE001
                bands[f"render_error:{exc!r}"] += 1
            lat = canonical.get("grading_latency_ms")
            if lat is not None:
                latencies.append(float(lat))

        graph = graph_payload_for(artifact_canonical=canonical, artifact_pair=pair)
        gsub = per_subject_graph.setdefault(subj, [0, 0])  # [graph_rows, ok_attempts]
        gsub[1] += 1
        if graph:
            graph_n += 1
            gsub[0] += 1
            ab = graph.get("abstention") or {}
            if ab.get("abstained"):
                abstained_n += 1
            else:
                not_abstained_n += 1
            # persisted artifact stores the reason list under "reasons"
            # (artifact pair payload shape), not the apply_abstention
            # dataclass field name "abstention_reasons"
            for r in ab.get("reasons") or ab.get("abstention_reasons") or []:
                reasons_hist[r] += 1

    print("=== per-subject status counts ===")
    for (subj, status), n in sorted(by_subject_status.items()):
        print(f"  {subj:20s} {status:6s} {n}")

    print(f"\n=== artifact rows === ok attempts with BOTH rows: {both_rows}")
    if missing_rows:
        print("  MISSING rows (persona_id, has_canonical, has_pair):")
        for row in missing_rows:
            print(f"    {row}")

    print("\n=== canonical composites (T1 fix evidence) ===")
    if composites:
        print(f"  n={len(composites)} min={min(composites):.3f} max={max(composites):.3f} "
              f"mean={sum(composites)/len(composites):.3f} zeros={sum(1 for c in composites if c == 0)}")
    print(f"  band distribution: {dict(sorted(bands.items(), key=lambda kv: str(kv[0])))}")

    print("\n=== graph/pair abstention (T2 fix evidence; F1b baseline 36/36=100%) ===")
    print(f"  graph rows: {graph_n}  abstained: {abstained_n}  not abstained: {not_abstained_n}")
    if graph_n:
        print(f"  abstention rate: {abstained_n/graph_n:.1%}")
    print(f"  reasons histogram: {dict(reasons_hist.most_common())}")

    print("\n=== graph-graded fraction per subject (graph_rows/ok) ===")
    for subj, (g, ok) in sorted(per_subject_graph.items()):
        print(f"  {subj:20s} {g}/{ok} = {g/ok:.1%}" if ok else f"  {subj}: no ok attempts")

    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, int(round(0.95 * len(latencies))) - 1)] if len(latencies) > 1 else latencies[0]
        print(f"\n=== grading latency === n={len(latencies)} p95={p95:.0f}ms max={max(latencies):.0f}ms")


if __name__ == "__main__":
    main()
