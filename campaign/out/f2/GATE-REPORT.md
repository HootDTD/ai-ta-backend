# Gate Report — run `f2`

**Config SHA:** `11197bb69d8ae7f5559d27ad5b251fce83d19eec9263e570a5a39e2a88cbb386`
**Overall:** FAIL

## Gates

| Gate | Result | Value | Bar |
|---|---|---|---|
| s1_reference_graph | FAIL | 0.825 | 0.950 |
| s2_ingestion | FAIL | 0.750 | 0.950 |
| s3_student_fidelity | FAIL | 0.639 | 0.950 |
| s4_apollo_coherence | FAIL | 0.412 | 0.900 |
| s5_misconceptions | FAIL | 0.000 | 0.900 |
| adjudication | FAIL | 0.000 | 0.950 |
| graph_graded:fluid_mechanics | FAIL | 0.000 | 0.700 |
| graph_graded:linear_motion | FAIL | 0.000 | 0.700 |
| graph_graded:macroeconomics | FAIL | 0.000 | 0.700 |
| ops | FAIL | 15609.000 | 15000.000 |
| breadth | FAIL | 3.000 | 4.000 |

## Failures (next work queue)

- **s1_reference_graph**: s1_reference_graph: 165/200 = 82.5% (bar 95%)
- **s2_ingestion**: s2_ingestion: 3/4 = 75.0% (bar 95%)
- **s3_student_fidelity**: s3_student_fidelity: 407/637 = 63.9% (bar 95%)
- **s4_apollo_coherence**: s4_apollo_coherence: 14/34 = 41.2% (bar 90%)
- **s5_misconceptions**: s5_misconceptions: 0/1 = 0.0% (bar 90%)
- **adjudication**: adjudication: 0 packets sampled — treated as failing
- **graph_graded:fluid_mechanics**: fluid_mechanics: 0.0% graph-graded (0/16, bar 70%)
- **graph_graded:linear_motion**: linear_motion: 0.0% graph-graded (0/4, bar 70%)
- **graph_graded:macroeconomics**: macroeconomics: 0.0% graph-graded (0/18, bar 70%)
- **ops**: ops: p95=15609ms (bar <= 15000ms), 0 event-loop-stall warning(s)
- **breadth**: breadth: 3 subjects (bar >= 4), wu_aas=yes, held_out=no

## Paired graph-vs-LLM comparison

- Pairs compared: 34 (skipped, missing pair: 4)
- **Band agreement rate (primary paired metric): 11.8%**
- Mean raw composite delta (graph - llm): -0.4969 — **informational / cross-scale only**: the graph composite is coverage-weighted and the LLM composite is rubric-derived, so they sit on different scales and this delta is a review finding, not a gate signal (see paired_comparison() docstring).

| Attempt | Subject | Graph | LLM | Delta | Bands |
|---|---|---|---|---|---|
| 40 | macroeconomics | 0.000 | 0.950 | -0.950 | Beginning vs Strong |
| 1 | fluid_mechanics | 0.000 | 0.930 | -0.930 | Beginning vs Strong |
| 45 | macroeconomics | 0.150 | 0.950 | -0.800 | Beginning vs Strong |
| 48 | macroeconomics | 0.240 | 1.000 | -0.760 | Beginning vs Strong |
| 36 | macroeconomics | 0.150 | 0.900 | -0.750 | Beginning vs Strong |
| 42 | macroeconomics | 0.200 | 0.930 | -0.730 | Beginning vs Strong |
| 15 | fluid_mechanics | 0.240 | 0.960 | -0.720 | Beginning vs Strong |
| 34 | macroeconomics | 0.240 | 0.950 | -0.710 | Beginning vs Strong |
| 43 | macroeconomics | 0.240 | 0.950 | -0.710 | Beginning vs Strong |
| 25 | fluid_mechanics | 0.300 | 1.000 | -0.700 | Beginning vs Strong |
