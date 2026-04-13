# Apollo Week 2 Spike Report

**Completed:** <Fri, Apr 24>
**Author:** <you>

## Executive summary

One paragraph: did all three spikes pass? If not, what failed, and what is the recommended response?

## Spike A — Parser accuracy

**Dataset:** <N> utterances from <sources>.
**Result:**
- Correctly extracted: <%>
- Silent failure: <%>
- Rejected cleanly: <%>
- Missed: <%>

**Threshold (≥60% correct, ≤10% silent):** PASS | FAIL.

**Top failure modes:**
1. …
2. …

**Example failures:**
> Utterance: "…"
> Parser output: …
> Expected: …
> Diagnosis: …

## Spike B — Leakage suite

**Probes run:** 20.
**Leaks on iteration 1:** <N>.
**Leaks on iteration 2 (if needed):** <N>.
**Leaks on iteration 3 (if needed):** <N>.
**Final:** PASS (0 leaks) | FAIL (any leak after 3 iterations).

**Leaky probes (if any) and fixes applied:**
1. …

## Spike C — Student UX

**Students run:** <N>.
**"Would you use this" (unprompted yes):** <N of M>.
**"Identified a pedagogically valuable moment":** <N of M>.
**Negative framings ("creepy/pointless/chore"):** <count + quotes>.

**Threshold (≥3/5 yes AND ≥3/5 valuable moment AND 0 negative):** PASS | FAIL.

**Representative quotes:**
> "…"
> "…"

**Observations from recordings:**
1. …
2. …

## Go/no-go decision

- [ ] Spike A passes → proceed to Week 3 parser v1 as planned
- [ ] Spike A fails → fallback to template-guided teaching OR narrow parser scope (decision: …)
- [ ] Spike B passes → proceed to Week 3 with current Apollo prompt structure
- [ ] Spike B fails → architecture change required before Week 3: retrieval-only Apollo OR rule-based dialogue (decision: …)
- [ ] Spike C passes → proceed to Week 3 with confidence in the pedagogical premise
- [ ] Spike C fails → existential pause; reconsider interaction model

**All three pass → Week 3 begins Monday.**
**Any fail → pause, document decision, re-plan.**

## Learnings for Week 3 architecture

- Parser design changes informed by Spike A: …
- Prompt design changes informed by Spike B: …
- UX design changes informed by Spike C: …
- Content gaps identified (problems, DAG, variable map): …
