# NLI Grading Probe — Resolution Results

**Date:** 2026-06-30  **Model:** `cross-encoder/nli-deberta-v3-large`  **Device:** `cpu`

**NLI params:**
- `min_entailment` = 0.87
- `max_contradiction` = 0.1
- `misconception_veto_entailment` = 0.8
- `ambiguity_margin` = 0.1
- `top_k` = 5

## Summary

| Problem | NLI-elig | UR OFF | UR ON | Recovered | Vetoed | FalseCredit |
|---------|----------|--------|-------|-----------|--------|-------------|
| bernoulli_01 | 5 | 0.7500 | 0.2500 | 2 | True | 0 |
| econ_a_gdp_components_01 | 3 | 0.7500 | 0.7500 | 0 | True | 0 |
| econ_b_real_gdp_02 | 3 | 0.7500 | 0.5000 | 1 | False | 0 |

## Problem: `bernoulli_01`

| node_id | type | intended_key | student_surface (truncated) | TSR-vs-display | floor | OFF method→key | ON method→key | NLI ent | NLI con | label | notes |
|---------|------|-------------|----------------------------|---------------|-------|----------------|---------------|---------|---------|-------|-------|
| b_nli_cond | cond | `cond.incompressibility` | the incompressibility assumption means the fluid preserves c… | 0.526 | Y | unresolved→– | nli→cond.incompressibility | 0.9739 | 0.0008 | entailment | NLI scores PASS threshold (ent=0.9739>=0.87, con=0.0008<=0.1) |
| b_nli_simp | simp | `simp.horizontal_simplification` | the conduit has no elevation change so height is the same at… | 0.537 | Y | unresolved→– | nli→simp.horizontal_simplific | 0.9953 | 0.0001 | entailment | NLI scores PASS threshold (ent=0.9953>=0.87, con=0.0001<=0.1) |
| b_ctrl | cond | `cond.incompressibility` | density is constant… | 0.383 | N | exact→cond.incompressibility | exact→cond.incompressibility | – | – | – | – |
| b_misc | misc | `misc.pressure_velocity_same_direction` | fluid pressure climbs higher as the flow velocity increases… | 0.447 | Y | unresolved→– | unresolved→– | 0.9545 | 0.0001 | entailment | VETO FIRED ent=0.9545 >= 0.8 |

### Node notes

- **b_nli_cond** [NLI] floor=PASS recovered=True false_credit=False veto=False
  - NLI premise:     `the incompressibility assumption means the fluid preserves constant density as it moves through the pipe`
  - NLI hypothesis:  `Incompressibility assumption`
  - Scores: ent=0.9739 con=0.0008 neu=0.0253 → label=**entailment**
  - Notes: NLI scores PASS threshold (ent=0.9739>=0.87, con=0.0008<=0.1)
- **b_nli_simp** [NLI] floor=PASS recovered=True false_credit=False veto=False
  - NLI premise:     `the conduit has no elevation change so height is the same at both cross-sections gravity-driven head terms vanish from t`
  - NLI hypothesis:  `Horizontal pipe: both sections are at the same height, so the gravitational potential-energy terms cancel`
  - Scores: ent=0.9953 con=0.0001 neu=0.0046 → label=**entailment**
  - Notes: NLI scores PASS threshold (ent=0.9953>=0.87, con=0.0001<=0.1)
- **b_ctrl** [CTRL] floor=FAIL recovered=False false_credit=False veto=False
- **b_misc** [MISC] floor=PASS recovered=False false_credit=False veto=True
  - NLI premise:     `fluid pressure climbs higher as the flow velocity increases`
  - NLI hypothesis:  `faster flow means higher pressure`
  - Scores: ent=0.9545 con=0.0001 neu=0.0454 → label=**entailment**
  - Notes: VETO FIRED ent=0.9545 >= 0.8

## Problem: `econ_a_gdp_components_01`

| node_id | type | intended_key | student_surface (truncated) | TSR-vs-display | floor | OFF method→key | ON method→key | NLI ent | NLI con | label | notes |
|---------|------|-------------|----------------------------|---------------|-------|----------------|---------------|---------|---------|-------|-------|
| ea_nli_cond | cond | `cond.final_goods_only` | only final goods and services are tallied; intermediate inpu… | 0.885 | Y | unresolved→– | unresolved→– | 0.7228 | 0.0008 | entailment | NLI scores BELOW threshold (ent=0.7228 need>=0.87; con=0.0008 need<=0.1) |
| ea_nli_proc | proc | `proc.compute_net_exports` | take the gap between foreign sales and purchases to find the… | 0.449 | Y | unresolved→– | unresolved→– | 0.0006 | 0.0294 | neutral | NLI scores BELOW threshold (ent=0.0006 need>=0.87; con=0.0294 need<=0.1) |
| ea_ctrl | cond | `cond.final_goods_only` | only final goods and services produced this year are counted… | 0.885 | Y | exact→cond.final_goods_only | exact→cond.final_goods_only | – | – | – | – |
| ea_misc | misc | `misc.includes_transfers` | welfare disbursements and resale transactions ought to be ad… | 0.368 | Y | unresolved→– | unresolved→– | 0.9968 | 0.0002 | entailment | VETO FIRED ent=0.9968 >= 0.8 |

### Node notes

- **ea_nli_cond** [NLI] floor=PASS recovered=False false_credit=False veto=False
  - NLI premise:     `only final goods and services are tallied; intermediate inputs, second-hand sales, and transfer payments are omitted`
  - NLI hypothesis:  `Final goods and services only`
  - Scores: ent=0.7228 con=0.0008 neu=0.2765 → label=**entailment**
  - Notes: NLI scores BELOW threshold (ent=0.7228 need>=0.87; con=0.0008 need<=0.1)
- **ea_nli_proc** [NLI] floor=PASS recovered=False false_credit=False veto=False
  - NLI premise:     `take the gap between foreign sales and purchases to find the trade balance component`
  - NLI hypothesis:  `Compute net exports (the trade balance) by subtracting imports from exports`
  - Scores: ent=0.0006 con=0.0294 neu=0.9701 → label=**neutral**
  - Notes: NLI scores BELOW threshold (ent=0.0006 need>=0.87; con=0.0294 need<=0.1)
- **ea_ctrl** [CTRL] floor=PASS recovered=False false_credit=False veto=False
- **ea_misc** [MISC] floor=PASS recovered=False false_credit=False veto=True
  - NLI premise:     `welfare disbursements and resale transactions ought to be added to the GDP expenditure tally alongside new production`
  - NLI hypothesis:  `add transfers to gdp`
  - Scores: ent=0.9968 con=0.0002 neu=0.0031 → label=**entailment**
  - Notes: VETO FIRED ent=0.9968 >= 0.8

## Problem: `econ_b_real_gdp_02`

| node_id | type | intended_key | student_surface (truncated) | TSR-vs-display | floor | OFF method→key | ON method→key | NLI ent | NLI con | label | notes |
|---------|------|-------------|----------------------------|---------------|-------|----------------|---------------|---------|---------|-------|-------|
| eb_nli_def | def | `def.real_basis` | real GDP strips out inflation so it captures only the volume… | 0.600 | Y | unresolved→– | nli→def.real_basis | 0.9887 | 0.001 | entailment | NLI scores PASS threshold (ent=0.9887>=0.87, con=0.0010<=0.1) |
| eb_nli_proc | proc | `proc.compute_real_change` | find the gap between the later and earlier output figures to… | 0.650 | Y | unresolved→– | unresolved→– | 0.0005 | 0.0002 | neutral | NLI scores BELOW threshold (ent=0.0005 need>=0.87; con=0.0002 need<=0.1) |
| eb_ctrl | misc | `misc.nominal_for_real` | nominal gdp is the same as real gdp… | 0.769 | Y | alias→misc.nominal_for_real | alias→misc.nominal_for_real | – | – | – | – |
| eb_misc | misc | `misc.nominal_for_real` | current dollar output tells us as much as the inflation-corr… | 0.232 | N | unresolved→– | unresolved→– | 0.0127 | 0.0111 | neutral | veto did NOT fire ent=0.0127 < 0.8 |

### Node notes

- **eb_nli_def** [NLI] floor=PASS recovered=True false_credit=False veto=False
  - NLI premise:     `real GDP strips out inflation so it captures only the volume increase in goods and services produced`
  - NLI hypothesis:  `Real GDP is inflation-adjusted, so a change in it reflects a change in the quantity of output produced`
  - Scores: ent=0.9887 con=0.001 neu=0.0102 → label=**entailment**
  - Notes: NLI scores PASS threshold (ent=0.9887>=0.87, con=0.0010<=0.1)
- **eb_nli_proc** [NLI] floor=PASS recovered=False false_credit=False veto=False
  - NLI premise:     `find the gap between the later and earlier output figures to isolate the absolute real change`
  - NLI hypothesis:  `Compute the absolute change in real GDP by subtracting the earlier value from the later value`
  - Scores: ent=0.0005 con=0.0002 neu=0.9993 → label=**neutral**
  - Notes: NLI scores BELOW threshold (ent=0.0005 need>=0.87; con=0.0002 need<=0.1)
- **eb_ctrl** [CTRL] floor=PASS recovered=False false_credit=False veto=False
- **eb_misc** [MISC] floor=FAIL recovered=False false_credit=False veto=False
  - NLI premise:     `current dollar output tells us as much as the inflation-corrected figure would since prices preserve the production tota`
  - NLI hypothesis:  `nominal gdp is the same as real gdp`
  - Scores: ent=0.0127 con=0.0111 neu=0.9762 → label=**neutral**
  - Notes: veto did NOT fire ent=0.0127 < 0.8

## Key Findings

1. **Content-token floor structural limitation**: Reference candidates whose `display_name` is the canonical key (e.g. `proc.compute_net_exports`, `simp.horizontal_simplification`, `def.real_basis`) have a single-token display surface that student paraphrases cannot share content tokens with. The NLI tier's `_content_tokens(student) & _content_tokens(sc.text) = empty` guard blocks NLI before it ever calls the model for these nodes.

2. **Human-readable display names enable NLI recovery**: Only candidates with human-readable labels (e.g. `cond.incompressibility` → "Incompressibility assumption") can pass the floor — see per-node scores above.

3. **Misconception veto**: See per-problem veto_fired status above.

4. **False credits**: Any node where ON resolved to the WRONG key is flagged; count above.
