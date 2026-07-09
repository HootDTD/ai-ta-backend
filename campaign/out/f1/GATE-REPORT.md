# Gate Report — run `f1`

**Config SHA:** `0dc814456be1bdc46f4a32f9306306c626d1fb5fc598643996a3ba494e62cded`
**Overall:** FAIL

## Gates

| Gate | Result | Value | Bar |
|---|---|---|---|
| s1_reference_graph | FAIL | 0.731 | 0.950 |
| s2_ingestion | FAIL | 0.500 | 0.950 |
| s3_student_fidelity | FAIL | 0.612 | 0.950 |
| s4_apollo_coherence | FAIL | 0.306 | 0.900 |
| s5_misconceptions | FAIL | 0.000 | 0.900 |
| adjudication | FAIL | 0.000 | 0.950 |
| graph_graded:fluid_mechanics | FAIL | 0.000 | 0.700 |
| graph_graded:linear_motion | FAIL | 0.000 | 0.700 |
| graph_graded:macroeconomics | FAIL | 0.000 | 0.700 |
| ops | FAIL | 20969.000 | 15000.000 |
| breadth | FAIL | 3.000 | 4.000 |

## Failures (next work queue)

- **s1_reference_graph**: s1_reference_graph: 160/219 = 73.1% (bar 95%)
- **s2_ingestion**: s2_ingestion: 2/4 = 50.0% (bar 95%)
- **s3_student_fidelity**: s3_student_fidelity: 383/626 = 61.2% (bar 95%)
- **s4_apollo_coherence**: s4_apollo_coherence: 11/36 = 30.6% (bar 90%)
- **s5_misconceptions**: s5_misconceptions: 0/0 = 0.0% (bar 90%) — zero items audited, treated as failing
- **adjudication**: adjudication: 0 packets sampled — treated as failing
- **graph_graded:fluid_mechanics**: fluid_mechanics: 0.0% graph-graded (0/16, bar 70%)
- **graph_graded:linear_motion**: linear_motion: 0.0% graph-graded (0/4, bar 70%)
- **graph_graded:macroeconomics**: macroeconomics: 0.0% graph-graded (0/18, bar 70%)
- **ops**: ops: p95=20969ms (bar <= 15000ms), 0 event-loop-stall warning(s)
- **breadth**: breadth: 3 subjects (bar >= 4), wu_aas=yes, held_out=no

## Paired graph-vs-LLM comparison

- Pairs compared: 36 (skipped, missing pair: 2)
- Band agreement rate: 77.8%
- Mean delta (graph - llm): 0.3287

| Attempt | Subject | Graph | LLM | Delta | Bands |
|---|---|---|---|---|---|
| 28 | fluid_mechanics | 0.725 | 0.000 | +0.725 | Proficient vs Beginning |
| 48 | macroeconomics | 0.694 | 0.000 | +0.694 | Developing vs Beginning |
| 60 | macroeconomics | 0.671 | 0.000 | +0.671 | Developing vs Beginning |
| 53 | macroeconomics | 0.662 | 0.000 | +0.662 | Developing vs Beginning |
| 56 | macroeconomics | 0.662 | 0.000 | +0.662 | Developing vs Beginning |
| 58 | macroeconomics | 0.662 | 0.000 | +0.662 | Developing vs Beginning |
| 63 | macroeconomics | 0.662 | 0.000 | +0.662 | Developing vs Beginning |
| 62 | macroeconomics | 0.542 | 0.000 | +0.542 | Developing vs Beginning |
| 30 | fluid_mechanics | 0.450 | 0.000 | +0.450 | Beginning vs Beginning |
| 59 | macroeconomics | 0.450 | 0.000 | +0.450 | Beginning vs Beginning |
