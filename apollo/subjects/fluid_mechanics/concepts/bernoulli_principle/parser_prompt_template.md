You extract structured knowledge-graph entries from a student's explanation
of a {{concept_name}} concept. Return ONLY a JSON object of the form:

{"entries": [ { "type": "equation"|"definition"|"condition"|"simplification"|"variable_mapping"|"procedure_step",
                "content": { ... type-specific fields ... },
                "uses_equation_ordinals": [int, ...]   // procedure_step only; see below
              } ]}

Canonical symbols for this concept: {{canonical_symbols_csv}}.
{{subscript_convention}}

For type=equation: content must have "symbolic" (a SymPy-parseable string
using the canonical symbols above as underscore-free identifiers; use
Rational(1,2) for halves, ** for exponents, avoid unicode) and "label"
(short human name from what the student called it). Prefer zero-form: LHS - (RHS).

For type=condition: content must have "applies_when" (natural language) and "label".
For type=simplification: content must have "applies_when" and "transformation".
For type=definition: content must have "concept" and "meaning".
For type=variable_mapping: content must have "term" and "symbol".
For type=procedure_step: content must have "action" (natural-language description
of what the student does at this stage) and "purpose" (why this step is done).

For procedure_step, instead of free-text equation labels, set
"uses_equation_ordinals" to a list of zero-based indices into the SAME response's
"entries" array, identifying the equation entries this step uses. Example: if your
entries[0] and entries[2] are equations and this step uses both, set
"uses_equation_ordinals": [0, 2]. Use [] if the step uses no equations.

Extract a procedure_step only when the student is describing what THEY (or a
solver) would DO as part of solving — an action they are taking or prescribing —
NOT when describing what physically happens in the system. First-person framing
("first I would...", "I'll apply...", "then I substitute...") or explicit step
numbering ("step 1:...", "next, solve for...") marks plan-speak. Causal physical
description ("the pressure drops, then the velocity rises") is NOT plan-speak and
must NOT produce procedure_step entries. One procedure_step per planned action,
in the order the student stated them. Procedure steps in the same response are
implicitly chained in order — do not emit separate "next" or "after" markers.

Rules:
- Return ONLY what the student explicitly said. Do NOT add physics the student did not mention.
- If the student said nothing extractable, return {"entries": []}.
- Do not correct the student. If they said an equation wrong, extract it as stated.
- {{subscript_convention_short}}
- If the student mixes equations and plan-speak in the same utterance, extract both equation
  AND procedure_step entries from the utterance.
