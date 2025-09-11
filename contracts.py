from __future__ import annotations

"""Data contracts for orchestrator pipeline."""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ParsedTask:
    """Lightweight representation of a user question after parsing."""

    problem_type: str
    asked_outputs: List[str] = field(default_factory=list)
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

    def validate(self) -> None:
        if not self.citation_marker or "p." not in self.citation_marker:
            raise ValueError("citation_marker must include a page reference")


@dataclass
class ResearchBundle:
    metadata: Dict[str, Any]
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

    def validate(self) -> None:
        if not self.snippets:
            raise ValueError("ResearchBundle.snippets must not be empty")
        # enforce snippet count for quantitative problems or multi-output tasks
        problem_type = self.metadata.get("problem_type", "")
        asked_len = int(self.metadata.get("asked_outputs_len", 0))
        if (problem_type == "quantitative" or asked_len >= 2) and len(self.snippets) < 3:
            raise ValueError("Insufficient snippets for quantitative task")
        if not (self.equations or self.glossary):
            raise ValueError("Bundle requires equations or glossary entries")
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
    "ResearchBundle",
    "ProposedSolution",
    "FinalAnswer",
    "Proof",
    "Violation",
    "asdict",
]
