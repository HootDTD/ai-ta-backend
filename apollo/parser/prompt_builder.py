"""Build a parser system prompt from a concept's registry template.

The template lives at apollo/subjects/<subject>/concepts/<concept>/parser_prompt_template.md
and accepts a {{concept_name}} substitution. v1 (diff-at-Done): the parser
captures the student's own form, so the canonical-symbol / subscript slots
were removed from the template and are no longer substituted here.
"""
from __future__ import annotations

from apollo.subjects import ConceptDefinition


def build_system_prompt(concept: ConceptDefinition, *, concept_name: str | None = None) -> str:
    template = concept.parser_prompt_template
    return template.replace(
        "{{concept_name}}", concept_name or concept.concept_id.replace("_", " ")
    )
