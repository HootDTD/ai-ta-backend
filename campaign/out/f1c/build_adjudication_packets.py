"""F1b ad-hoc driver: select the stratified Fable adjudication sample and
write campaign/out/f1/adjudication-packets.jsonl.

Contract (task brief): 12 attempts stratified across subjects, archetypes and
band levels, >=2 abstained (graph shadow abstained) and >=1 errored attempt
if any exist. Each packet line carries ONLY the adjudicator's minimal
contract: attempt_id, subject, persona archetype, the rendered scorecard
dict, composite+band from BOTH graders, and the abstention block.

Deterministic (seeded) so a re-run reproduces the same sample.

Usage: python -m campaign.out.f1.build_adjudication_packets
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from apollo.projections.scorecard import render_scorecard  # noqa: E402
from campaign.adapters import graph_payload_for, llm_payload_for  # noqa: E402
from campaign.judges.base import load_jsonl  # noqa: E402
from campaign.report import band_for_score  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
SAMPLE_SIZE = 12
MIN_ABSTAINED = 2
SEED = 20260702


def packet_for(att: dict) -> dict:
    canonical = att.get("artifact_canonical")
    pair = att.get("artifact_pair")
    graph = graph_payload_for(artifact_canonical=canonical, artifact_pair=pair)
    llm = llm_payload_for(artifact_canonical=canonical, artifact_pair=pair)

    def grader_block(payload):
        if not payload:
            return None
        composite = (payload.get("scores") or {}).get("composite")
        return {
            "composite": composite,
            "band": band_for_score(float(composite)) if composite is not None else None,
        }

    scorecard = None
    if canonical:
        try:
            scorecard = render_scorecard(dict(canonical))
        except Exception as exc:  # noqa: BLE001 -- errored attempts have no artifact
            scorecard = {"render_error": repr(exc)[:200]}

    return {
        "attempt_id": att.get("attempt_id"),
        "subject": att.get("subject"),
        "persona_archetype": att.get("persona"),
        "scorecard": scorecard,
        "graph": grader_block(graph),
        "llm": grader_block(llm),
        "abstention": (graph or {}).get("abstention"),
        "status": att.get("status"),
        "error": att.get("error"),
    }


def is_abstained(att: dict) -> bool:
    graph = graph_payload_for(
        artifact_canonical=att.get("artifact_canonical"), artifact_pair=att.get("artifact_pair")
    )
    return bool((graph or {}).get("abstention", {}).get("abstained"))


def band_of(att: dict) -> str | None:
    canonical = att.get("artifact_canonical")
    if not canonical:
        return None
    try:
        return render_scorecard(dict(canonical)).get("band")
    except Exception:  # noqa: BLE001
        return None


def stratified_sample(attempts: list[dict]) -> list[dict]:
    rng = random.Random(SEED)
    picked: list[dict] = []
    picked_ids: set = set()

    def take(att: dict) -> None:
        key = att.get("attempt_id") or att.get("persona_id")
        if key in picked_ids:
            return
        picked.append(att)
        picked_ids.add(key)

    errored = [a for a in attempts if a.get("status") == "error"]
    if errored:
        take(rng.choice(errored))  # >=1 errored-if-any

    abstained = [a for a in attempts if a.get("status") == "ok" and is_abstained(a)]
    rng.shuffle(abstained)
    for att in abstained[:MIN_ABSTAINED]:
        take(att)

    # F1c brief addition: >=1 clarification-heavy attempt (a non-empty
    # clarification_trace on the graph payload -- the loop actually fired).
    def is_clarification_heavy(att: dict) -> bool:
        graph = graph_payload_for(
            artifact_canonical=att.get("artifact_canonical"),
            artifact_pair=att.get("artifact_pair"),
        )
        return bool((graph or {}).get("clarification_trace"))

    clarifying = [a for a in attempts if a.get("status") == "ok" and is_clarification_heavy(a)]
    if clarifying and not any(is_clarification_heavy(a) for a in picked):
        clarifying.sort(
            key=lambda a: len(
                (
                    graph_payload_for(
                        artifact_canonical=a.get("artifact_canonical"),
                        artifact_pair=a.get("artifact_pair"),
                    )
                    or {}
                ).get("clarification_trace")
                or []
            ),
            reverse=True,
        )
        take(clarifying[0])

    # Round-robin over (subject, archetype) strata, preferring unseen bands.
    ok_attempts = [a for a in attempts if a.get("status") == "ok"]
    strata: dict[tuple, list[dict]] = {}
    for att in ok_attempts:
        strata.setdefault((att.get("subject"), att.get("persona")), []).append(att)
    for group in strata.values():
        rng.shuffle(group)

    seen_bands: set = {band_of(a) for a in picked}
    stratum_keys = sorted(strata, key=lambda k: (k[0] or "", k[1] or ""))
    while len(picked) < SAMPLE_SIZE and any(strata.values()):
        for key in stratum_keys:
            group = strata.get(key) or []
            if not group:
                continue
            # prefer an attempt whose band we have not sampled yet
            idx = next((i for i, a in enumerate(group) if band_of(a) not in seen_bands), 0)
            att = group.pop(idx)
            k = att.get("attempt_id") or att.get("persona_id")
            if k in picked_ids:
                continue
            take(att)
            seen_bands.add(band_of(att))
            if len(picked) >= SAMPLE_SIZE:
                break
    return picked[:SAMPLE_SIZE]


def main() -> None:
    attempts = load_jsonl(OUT_DIR / "attempts.jsonl")
    sample = stratified_sample(attempts)
    out_path = OUT_DIR / "adjudication-packets.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for att in sample:
            fh.write(json.dumps(packet_for(att), sort_keys=True, default=str))
            fh.write("\n")
    print(f"wrote {len(sample)} packets to {out_path}")
    for att in sample:
        print(
            f"  attempt_id={att.get('attempt_id')} subject={att.get('subject')} "
            f"archetype={att.get('persona')} status={att.get('status')} "
            f"band={band_of(att)} abstained={is_abstained(att)}"
        )


if __name__ == "__main__":
    main()
