# NLI Grading Probe — Resolution Results

**Date:** 2026-06-30  **Model:** `cross-encoder/nli-deberta-v3-small`  **Device:** `cpu`

**NLI params:**
- `min_entailment` = 0.87
- `max_contradiction` = 0.1
- `misconception_veto_entailment` = 0.8
- `ambiguity_margin` = 0.1
- `top_k` = 5

## Summary

| Problem | NLI-elig | UR OFF | UR ON | Recovered | Vetoed | FalseCredit |
|---------|----------|--------|-------|-----------|--------|-------------|
| bernoulli_01 | 5 | 0.7500 | 0.5000 | 1 | False | 0 |
| econ_a_gdp_components_01 | 3 | 0.7500 | 0.5000 | 1 | True | 0 |
| econ_b_real_gdp_02 | 3 | 0.7500 | 0.7500 | 0 | False | 0 |

## Problem: `bernoulli_01`

| node_id | type | intended_key | student_surface (truncated) | TSR-vs-display | floor | OFF method→key | ON method→key | NLI ent | NLI con | label | notes |
|---------|------|-------------|----------------------------|---------------|-------|----------------|---------------|---------|---------|-------|-------|
| b_nli_cond | cond | `cond.incompressibility` | the incompressibility assumption means the fluid preserves c… | 0.526 | Y | unresolved→– | nli→cond.incompressibility | 0.9738 | 0.0032 | entailment | NLI scores PASS threshold (ent=0.9738>=0.87, con=0.0032<=0.1) |
| b_nli_simp | simp | `simp.horizontal_simplification` | the conduit has no elevation change so height is the same at… | 0.212 | N | unresolved→– | unresolved→– | 0.0782 | 0.0749 | neutral | CONTENT-TOKEN FLOOR FAILS: student&display('simp.horizontal_simplification')=emp |
| b_ctrl | cond | `cond.incompressibility` | density is constant… | 0.383 | N | exact→cond.incompressibility | exact→cond.incompressibility | – | – | – | – |
| b_misc | misc | `misc.pressure_velocity_same_direction` | fluid pressure climbs higher as the flow velocity increases… | 0.447 | Y | unresolved→– | unresolved→– | 0.1404 | 0.0016 | neutral | veto did NOT fire ent=0.1404 < 0.8 |

### Node notes

- **b_nli_cond** [NLI] floor=PASS recovered=True false_credit=False veto=False
  - NLI premise:     `the incompressibility assumption means the fluid preserves constant density as it moves through the pipe`
  - NLI hypothesis:  `Incompressibility assumption`
  - Scores: ent=0.9738 con=0.0032 neu=0.023 → label=**entailment**
  - Notes: NLI scores PASS threshold (ent=0.9738>=0.87, con=0.0032<=0.1)
- **b_nli_simp** [NLI] floor=FAIL recovered=False false_credit=False veto=False
  - NLI premise:     `the conduit has no elevation change so height is the same at both cross-sections gravity-driven head terms vanish from t`
  - NLI hypothesis:  `simp.horizontal_simplification`
  - Scores: ent=0.0782 con=0.0749 neu=0.8469 → label=**neutral**
  - Notes: CONTENT-TOKEN FLOOR FAILS: student&display('simp.horizontal_simplification')=empty -- NLI tier structurally cannot certify this node
- **b_ctrl** [CTRL] floor=FAIL recovered=False false_credit=False veto=False
- **b_misc** [MISC] floor=PASS recovered=False false_credit=False veto=False
  - NLI premise:     `fluid pressure climbs higher as the flow velocity increases`
  - NLI hypothesis:  `faster flow means higher pressure`
  - Scores: ent=0.1404 con=0.0016 neu=0.858 → label=**neutral**
  - Notes: veto did NOT fire ent=0.1404 < 0.8

## Problem: `econ_a_gdp_components_01`

| node_id | type | intended_key | student_surface (truncated) | TSR-vs-display | floor | OFF method→key | ON method→key | NLI ent | NLI con | label | notes |
|---------|------|-------------|----------------------------|---------------|-------|----------------|---------------|---------|---------|-------|-------|
| ea_nli_cond | cond | `cond.final_goods_only` | only final goods and services are tallied; intermediate inpu… | 0.885 | Y | unresolved→– | nli→cond.final_goods_only | 0.9918 | 0.0005 | entailment | NLI scores PASS threshold (ent=0.9918>=0.87, con=0.0005<=0.1) |
| ea_nli_proc | proc | `proc.compute_net_exports` | take the gap between foreign sales and purchases to find the… | 0.250 | N | unresolved→– | unresolved→– | 0.0224 | 0.6776 | contradiction | CONTENT-TOKEN FLOOR FAILS: student&display('proc.compute_net_exports')=empty --  |
| ea_ctrl | cond | `cond.final_goods_only` | only final goods and services produced this year are counted… | 0.885 | Y | exact→cond.final_goods_only | exact→cond.final_goods_only | – | – | – | – |
| ea_misc | misc | `misc.includes_transfers` | welfare disbursements and resale transactions ought to be ad… | 0.368 | Y | unresolved→– | unresolved→– | 0.9578 | 0.0003 | entailment | VETO FIRED ent=0.9578 >= 0.8 |

### Node notes

- **ea_nli_cond** [NLI] floor=PASS recovered=True false_credit=False veto=False
  - NLI premise:     `only final goods and services are tallied; intermediate inputs, second-hand sales, and transfer payments are omitted`
  - NLI hypothesis:  `Final goods and services only`
  - Scores: ent=0.9918 con=0.0005 neu=0.0077 → label=**entailment**
  - Notes: NLI scores PASS threshold (ent=0.9918>=0.87, con=0.0005<=0.1)
- **ea_nli_proc** [NLI] floor=FAIL recovered=False false_credit=False veto=False
  - NLI premise:     `take the gap between foreign sales and purchases to find the trade balance component`
  - NLI hypothesis:  `proc.compute_net_exports`
  - Scores: ent=0.0224 con=0.6776 neu=0.3 → label=**contradiction**
  - Notes: CONTENT-TOKEN FLOOR FAILS: student&display('proc.compute_net_exports')=empty -- NLI tier structurally cannot certify this node
- **ea_ctrl** [CTRL] floor=PASS recovered=False false_credit=False veto=False
- **ea_misc** [MISC] floor=PASS recovered=False false_credit=False veto=True
  - NLI premise:     `welfare disbursements and resale transactions ought to be added to the GDP expenditure tally alongside new production`
  - NLI hypothesis:  `add transfers to gdp`
  - Scores: ent=0.9578 con=0.0003 neu=0.0419 → label=**entailment**
  - Notes: VETO FIRED ent=0.9578 >= 0.8

## Problem: `econ_b_real_gdp_02`

| node_id | type | intended_key | student_surface (truncated) | TSR-vs-display | floor | OFF method→key | ON method→key | NLI ent | NLI con | label | notes |
|---------|------|-------------|----------------------------|---------------|-------|----------------|---------------|---------|---------|-------|-------|
| eb_nli_def | def | `def.real_basis` | real GDP strips out inflation so it captures only the volume… | 0.175 | N | unresolved→– | unresolved→– | 0.0178 | 0.9388 | contradiction | CONTENT-TOKEN FLOOR FAILS: student&display('def.real_basis')=empty -- NLI tier s |
| eb_nli_proc | proc | `proc.compute_real_change` | find the gap between the later and earlier output figures to… | 0.220 | N | unresolved→– | unresolved→– | 0.0015 | 0.0037 | neutral | CONTENT-TOKEN FLOOR FAILS: student&display('proc.compute_real_change')=empty --  |
| eb_ctrl | misc | `misc.nominal_for_real` | nominal gdp is the same as real gdp… | 0.769 | Y | alias→misc.nominal_for_real | alias→misc.nominal_for_real | – | – | – | – |
| eb_misc | misc | `misc.nominal_for_real` | current dollar output tells us as much as the inflation-corr… | 0.232 | N | unresolved→– | unresolved→– | 0.0247 | 0.0099 | neutral | veto did NOT fire ent=0.0247 < 0.8 |

### Node notes

- **eb_nli_def** [NLI] floor=FAIL recovered=False false_credit=False veto=False
  - NLI premise:     `real GDP strips out inflation so it captures only the volume increase in goods and services produced`
  - NLI hypothesis:  `def.real_basis`
  - Scores: ent=0.0178 con=0.9388 neu=0.0435 → label=**contradiction**
  - Notes: CONTENT-TOKEN FLOOR FAILS: student&display('def.real_basis')=empty -- NLI tier structurally cannot certify this node
- **eb_nli_proc** [NLI] floor=FAIL recovered=False false_credit=False veto=False
  - NLI premise:     `find the gap between the later and earlier output figures to isolate the absolute real change`
  - NLI hypothesis:  `proc.compute_real_change`
  - Scores: ent=0.0015 con=0.0037 neu=0.9949 → label=**neutral**
  - Notes: CONTENT-TOKEN FLOOR FAILS: student&display('proc.compute_real_change')=empty -- NLI tier structurally cannot certify this node
- **eb_ctrl** [CTRL] floor=PASS recovered=False false_credit=False veto=False
- **eb_misc** [MISC] floor=FAIL recovered=False false_credit=False veto=False
  - NLI premise:     `current dollar output tells us as much as the inflation-corrected figure would since prices preserve the production tota`
  - NLI hypothesis:  `nominal gdp is the same as real gdp`
  - Scores: ent=0.0247 con=0.0099 neu=0.9654 → label=**neutral**
  - Notes: veto did NOT fire ent=0.0247 < 0.8

## Key Findings

1. **Content-token floor structural limitation**: Reference candidates whose `display_name` is the canonical key (e.g. `proc.compute_net_exports`, `simp.horizontal_simplification`, `def.real_basis`) have a single-token display surface that student paraphrases cannot share content tokens with. The NLI tier's `_content_tokens(student) & _content_tokens(sc.text) = empty` guard blocks NLI before it ever calls the model for these nodes.

2. **Human-readable display names enable NLI recovery**: Only candidates with human-readable labels (e.g. `cond.incompressibility` → "Incompressibility assumption") can pass the floor — see per-node scores above.

3. **Misconception veto**: See per-problem veto_fired status above.

4. **False credits**: Any node where ON resolved to the WRONG key is flagged; count above.
