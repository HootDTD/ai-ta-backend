from __future__ import annotations

"""Data contracts for orchestrator pipeline."""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ParsedTask:
    """Lightweight representation of a user question after parsing."""

    problem_type: str
    asked_outputs: List[str] = field(default_factory=list)
    asked_output_keys: List[str] = field(default_factory=list)
    knowns: Dict[str, Any] = field(default_factory=dict)
    constraints: List[str] = field(default_factory=list)
    figure_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.problem_type:
            raise ValueError("problem_type is required")
        if not (self.asked_outputs or self.knowns or self.constraints):
            raise ValueError(
                "ParsedTask must specify asked_outputs, knowns, or constraints"
            )


@dataclass
class BundleSnippet:
    id: str
    type: str
    page: int
    section_path: str
    text: str
    figure_id: Optional[str]
    why: str
    source_path: str
    doc_title: Optional[str]
    doc_short: str
    citation_marker: str
    final_score: Optional[Dict[str, float]] = None

    def validate(self) -> None:
        if not self.citation_marker or not self.citation_marker.strip():
            raise ValueError("citation_marker must not be empty")
        # Page reference (p. N) is preferred but not required — chunks without
        # a page_number (e.g. from slide decks or web content) produce markers
        # like "[Slides]" which are valid citations in the pgvector path.


@dataclass
class ResearchMetadata:
    doc_sets: List[str] = field(default_factory=list)
    loaded_indexes: List[str] = field(default_factory=list)
    question: str = ""
    problem_type: str = "unknown"
    asked_outputs_len: int = 0
    skipped_indexes: List[Dict[str, Any]] = field(default_factory=list)
    model: Optional[str] = None
    dimensions: Optional[int] = None
    k_sem: int = 0
    k_lex: int = 0
    token_budget: int = 0
    expansion_plan: List[Dict[str, Any]] = field(default_factory=list)
    aliases_used: Dict[str, int] = field(default_factory=dict)
    symbol_index_stats: Dict[str, Any] = field(default_factory=dict)
    coverage_gaps: List[str] = field(default_factory=list)
    refinement_queries: List[str] = field(default_factory=list)
    term_presence: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    missing_terms: List[str] = field(default_factory=list)
    expansion_candidates: Dict[str, List[str]] = field(default_factory=dict)
    iteration_trace: List[Dict[str, Any]] = field(default_factory=list)
    final_query: str = ""
    original_query: str = ""
    keyword_iterations: List[Dict[str, Any]] = field(default_factory=list)
    found_terms: List[str] = field(default_factory=list)
    not_found_terms: List[str] = field(default_factory=list)
    attempted_terms: List[str] = field(default_factory=list)
    concept_matches: Dict[str, List[str]] = field(default_factory=dict)
    concept_match_details: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    hit_count_sem: int = 0
    hit_count_lex: int = 0
    subject: str = "course/textbook"
    allowed_markers: List[str] = field(default_factory=list)


@dataclass
class ResearchBundle:
    metadata: ResearchMetadata
    snippets: List[BundleSnippet]
    equations: List[Dict[str, Any]] = field(default_factory=list)
    assumptions: List[Dict[str, Any]] = field(default_factory=list)
    glossary: List[Dict[str, Any]] = field(default_factory=list)
    known_values: List[Dict[str, Any]] = field(default_factory=list)
    boundary_conditions: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    coverage_gaps: List[str] = field(default_factory=list)
    refinement_queries: List[str] = field(default_factory=list)
    used_ids: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    allowed_markers: List[str] = field(default_factory=list)
    found_terms: List[str] = field(default_factory=list)
    not_found_terms: List[str] = field(default_factory=list)
    attempted_terms: List[str] = field(default_factory=list)
    subject: str = "course/textbook"
    marker_equal_map: Dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.snippets:
            raise ValueError("ResearchBundle.snippets must not be empty")
        # enforce snippet count for quantitative problems or multi-output tasks
        problem_type = getattr(self.metadata, "problem_type", "")
        asked_len = int(getattr(self.metadata, "asked_outputs_len", 0))
        if (problem_type == "quantitative" or asked_len >= 2) and len(self.snippets) < 3:
            raise ValueError("Insufficient snippets for quantitative task")
        if not (self.equations or self.glossary):
            raise ValueError("Bundle requires equations or glossary entries")
        if not self.allowed_markers:
            seen: List[str] = []
            seen_set: set[str] = set()
            for sn in self.snippets:
                marker = getattr(sn, "citation_marker", None)
                if isinstance(marker, str):
                    cleaned = marker.strip()
                    if cleaned and cleaned not in seen_set:
                        seen.append(cleaned)
                        seen_set.add(cleaned)
            if not seen:
                raise ValueError("Bundle requires allowed_markers")
            self.allowed_markers = seen
        for sn in self.snippets:
            sn.validate()


@dataclass
class ProposedSolution:
    steps: str
    final_answers: Dict[str, Any]
    equations_used: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    code: str | None = None
    code_output: str | None = None
    code_hash: str | None = None
    vars_created: List[str] = field(default_factory=list)


@dataclass
class FinalAnswer:
    text: str
    citations: List[str]


@dataclass
class Proof:
    question: str
    parsed_task: Dict[str, Any]
    research_bundle: Dict[str, Any]
    solver_output: Dict[str, Any]
    checks: Dict[str, Any]
    indexes_used: List[str]
    timestamps: Dict[str, Any]


@dataclass
class Violation(Exception):
    """Raised during validation phases when checks fail."""

    message: str
    field: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - simple repr
        if self.field:
            return f"{self.field}: {self.message}"
        return self.message


__all__ = [
    "ParsedTask",
    "BundleSnippet",
    "ResearchMetadata",
    "ResearchBundle",
    "ProposedSolution",
    "FinalAnswer",
    "Proof",
    "Violation",
    "asdict",
]

