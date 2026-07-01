#!/usr/bin/env python3
"""Analyze a tuning_results_<model>.json: recall curve, negative-entailment
safety margin, and the highest-threshold point that still holds max recall."""
from __future__ import annotations

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
pairs = data["reference_pairs"]
MAX_CON = 0.10

pos = [p for p in pairs if p["gold"] == "entailment"]
neg = [p for p in pairs if p["gold"] != "entailment"]


def credited(p, me, mc=MAX_CON):
    return p["_label"] == "entailment" and p["_ent"] >= me and p["_con"] <= mc


print(f"model: {data['model']}")
print(f"positives={len(pos)} negatives={len(neg)}  (max_con={MAX_CON})\n")

# Negative-entailment margin: highest ent among negatives that also pass con cap
neg_credit_risk = [
    (p["_ent"], p["node"], p["premise"][:50], p["hypothesis"][:45])
    for p in neg
    if p["_label"] == "entailment" and p["_con"] <= MAX_CON
]
neg_credit_risk.sort(reverse=True)
print("Top negative-entailment scores (the precision risk / FP margin):")
for ent, node, prem, hyp in neg_credit_risk[:6]:
    print(f"   ent={ent:.3f}  {node}")
    print(f"       '{prem}' -> '{hyp}'")
if not neg_credit_risk:
    print("   (no negative is even labeled 'entailment' under the con cap — margin is total)")
maxneg = neg_credit_risk[0][0] if neg_credit_risk else 0.0
print(f"\n=> highest negative entailment (con<= {MAX_CON}) = {maxneg:.3f}")
print(f"   any min_entailment > {maxneg:.3f} yields ZERO false positives on this dev set.\n")

print("Recall curve at max_con=0.10 (precision in parens):")
last_rec = None
robust = None
for me in [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.87, 0.90, 0.95]:
    tp = sum(1 for p in pos if credited(p, me))
    fp = sum(1 for p in neg if credited(p, me))
    prec = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
    rec = tp / len(pos)
    print(f"   min_ent={me:.2f}  recall={rec:.3f} ({tp}/{len(pos)})  precision={prec:.3f}  fp={fp}")

# Highest threshold that preserves the max achievable recall (precision>=0.95)
best_recall = 0.0
for me in [round(0.50 + i * 0.01, 2) for i in range(50)]:
    tp = sum(1 for p in pos if credited(p, me))
    fp = sum(1 for p in neg if credited(p, me))
    prec = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
    if prec >= 0.95:
        best_recall = max(best_recall, tp / len(pos))
# highest me achieving best_recall
robust_me = None
for me in [round(0.50 + i * 0.01, 2) for i in range(50)]:
    tp = sum(1 for p in pos if credited(p, me))
    fp = sum(1 for p in neg if credited(p, me))
    prec = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
    if prec >= 0.95 and abs(tp / len(pos) - best_recall) < 1e-9:
        robust_me = me
print(f"\nmax achievable recall @ precision>=0.95: {best_recall:.3f}")
print(f"HIGHEST min_entailment still holding that recall: {robust_me:.2f}")
print(f"  (robust choice: same recall as the floor-edge, maximal FP headroom)")
