# Authoring Apollo Problems

This guide explains the structure of a problem JSON file in
`apollo/problems/<concept>/problem_*.json` and how to add `procedure_step`
entries to the `reference_solution`.

## File structure

```json
{
  "id": "<slug>",
  "concept_id": "<concept>",
  "difficulty": "intro" | "standard" | "hard",
  "problem_text": "...",
  "given_values": { "<symbol>": <float> },
  "target_unknown": "<symbol>",
  "reference_solution": [ <ReferenceStep>, ... ]
}
```

Each `ReferenceStep` has `step`, `entry_type`, `id`, `content`, `depends_on`.

## Entry types

- `equation` — symbolic (SymPy-parseable, zero-form) + label + variables.
- `condition` — `applies_when` + label.
- `simplification` — `applies_when` + `transformation`.
- `definition` — `concept` + `meaning`.
- `variable_mapping` — `term` + `symbol`.
- `procedure_step` — `order` + `action` + `uses_equations` + `purpose`. **New in Teaching Rigor Phase 1.**

## Authoring `procedure_step` entries

A `procedure_step` captures *what the student should do at this stage to solve the problem*. It is graded by a semantic LLM matcher (not string match), with 0-1 partial credit per step.

**Fields:**
- `order` (int, >= 1): 1-based position of this step within the procedure. Must be contiguous across all `procedure_step` entries in this problem: `1, 2, 3, ...`. This is independent of the outer `step` field.
- `action` (str): natural-language description of what the student does. Reference specific equations and givens where possible.
- `uses_equations` (list of str): `id` values of `equation` entries in this problem that this step uses. If the step grounds in a `condition` or `simplification` rather than applying a named equation (e.g., a step whose purpose is "invoke the incompressibility assumption"), set `uses_equations` to `[]` and put the condition/simplification id in `depends_on` instead.
- `purpose` (str): one sentence explaining why this step is necessary.

**Worked example (from `bernoulli/problem_01.json`):**

```json
{
  "step": 5,
  "entry_type": "procedure_step",
  "id": "plan_apply_continuity",
  "content": {
    "order": 1,
    "action": "use the continuity equation with the known rho, A1, v1, and A2 to solve for v2",
    "uses_equations": ["continuity"],
    "purpose": "obtain v2 so it can be plugged into bernoulli at section 2"
  },
  "depends_on": ["continuity"]
}
```

**Rules of thumb:**
- 1-2 procedure steps for simple one-equation problems.
- 3-4 procedure steps for problems that chain continuity + bernoulli + a simplification.
- Each `procedure_step`'s `order` field is its 1-based index into the procedure, independent of the `step` field.
- The `step` field of a `procedure_step` entry should be the next integer after the last prior `step` in the file (the file's global ordering).
- `id` values must be unique within the file and should be prefixed `plan_`.

## Testing

After editing a problem JSON, run:

```bash
pytest apollo/schemas/tests/test_problem_schema.py -v --tb=short
```

This validates each file loads and checks `procedure_step` order contiguity.
