"""Build a parser system prompt from a concept's registry template.

The template lives at apollo/subjects/<subject>/concepts/<concept>/parser_prompt_template.md
and accepts {{slot}} substitutions for canonical_symbols_csv, subscript_convention,
subscript_convention_short, concept_name.
"""
from __future__ import annotations

from apollo.subjects import ConceptDefinition


def build_system_prompt(concept: ConceptDefinition, *, concept_name: str | None = None) -> str:
    sym = concept.canonical_symbols
    template = concept.parser_prompt_template

    sub_full = sym.subscript_convention or ""
    sub_short = "Use sensible subscripts (e.g. 1/2) when comparing two states." if sub_full else ""

    return (
        template
        .replace("{{concept_name}}", concept_name or concept.concept_id.replace("_", " "))
        .replace("{{canonical_symbols_csv}}", ", ".join(sym.symbols))
        .replace("{{subscript_convention}}", sub_full)
        .replace("{{subscript_convention_short}}", sub_short)
    )
