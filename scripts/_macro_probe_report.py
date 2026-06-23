"""Pure helpers for the Macro Ch.6 graph-grading probe orchestrator.

Everything here is pure (no DB, no subprocess, no HTTP, no env mutation) so it
is unit-testable in isolation: the local-DB target guard, the score-matrix
extraction from ``apollo_graph_comparison_runs`` rows, and the matrix-to-text
formatting the orchestrator prints + writes to its report JSON.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# The graph-sim score columns (apollo_graph_comparison_runs), in display order.
# Matches apollo/persistence/models.py::GraphComparisonRun.
SCORE_COLUMNS: tuple[str, ...] = (
    "coverage_score",
    "soundness_score",
    "bisimilarity_score",
    "node_coverage_score",
    "edge_coverage_score",
    "scoping_score",
    "usage_score",
    "procedure_order_score",
    "dependency_score",
    "contradiction_score",
)

_LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")


class LocalTargetError(RuntimeError):
    """Raised when a DB URL does not point at a local Postgres."""


def db_target(url: str) -> str:
    """Return the host[:port]/db portion of a SQLAlchemy URL (after the ``@``).

    Used only for display + the local-guard substring check, so it never has to
    fully parse credentials.
    """
    return (url or "").split("@")[-1]


def is_local_target(url: str) -> bool:
    """True iff ``url`` targets a local Postgres (127.0.0.1 / localhost)."""
    target = db_target(url)
    return any(host in target for host in _LOCAL_HOSTS)


def require_local_target(url: str) -> str:
    """Return ``url`` if local; else raise ``LocalTargetError``.

    Single chokepoint that keeps the orchestrator from ever booting a server or
    seeding against a remote Supabase project. Mirrors the index_local_pdf guard.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise LocalTargetError(
            "SUPABASE_DB_URL is not set — dot-source scripts/load_local_env.ps1 first."
        )
    if not is_local_target(cleaned):
        raise LocalTargetError(
            f"Refusing to run: DB target {db_target(cleaned)!r} is not local. "
            "run_macro_probe.py only targets a local Postgres (127.0.0.1 / localhost)."
        )
    return cleaned


def scores_from_run(run: Mapping[str, Any] | None) -> dict[str, float | None]:
    """Project one comparison-run row to the ordered score dict (+ ``abstained``).

    A ``None`` run (no comparison_runs produced for the attempt) yields all-``None``
    scores and ``abstained=None`` so the matrix still has a row for that cell.
    """
    if not run:
        cells: dict[str, float | None] = {col: None for col in SCORE_COLUMNS}
        cells["abstained"] = None
        return cells
    cells = {col: run.get(col) for col in SCORE_COLUMNS}
    cells["abstained"] = run.get("abstained")
    return cells


def build_score_matrix(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build the per-(problem, variation) score matrix from probe ``results``.

    Each result is one probe attempt dict (as written by ``apollo_grade_probe``):
    it must carry ``served_problem``, ``variation`` (or ``mode``), ``attempt_id``,
    and a ``graphsim_evidence.comparison_runs`` list. The first comparison run per
    attempt supplies the scores (one run per attempt in practice). Results that
    errored before grading still appear, with all-``None`` scores + their ``error``.
    """
    matrix: list[dict[str, Any]] = []
    for result in results:
        runs = (result.get("graphsim_evidence") or {}).get("comparison_runs") or []
        first_run = runs[0] if runs else None
        row: dict[str, Any] = {
            "problem": result.get("served_problem"),
            "variation": result.get("variation") or result.get("mode"),
            "attempt_id": result.get("attempt_id"),
            "error": result.get("error"),
            **scores_from_run(first_run),
        }
        matrix.append(row)
    return matrix


def _fmt_cell(value: Any) -> str:
    """Format a single score cell (round floats, blank for None)."""
    if value is None:
        return "."
    if isinstance(value, bool):
        return "T" if value else "F"
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return str(value)


def format_score_matrix(
    matrix: Sequence[Mapping[str, Any]],
    *,
    columns: Sequence[str] = SCORE_COLUMNS,
) -> str:
    """Render the score matrix as a fixed-width text table.

    One row per (problem, variation); one column per score plus ``abstained``.
    Pure string building — the orchestrator prints this and stores the structured
    ``matrix`` (not this text) in its report JSON.
    """
    short = {
        "coverage_score": "cov",
        "soundness_score": "snd",
        "bisimilarity_score": "bisim",
        "node_coverage_score": "ncov",
        "edge_coverage_score": "ecov",
        "scoping_score": "scope",
        "usage_score": "usage",
        "procedure_order_score": "order",
        "dependency_score": "dep",
        "contradiction_score": "contra",
        "abstained": "abst",
    }
    display_cols = list(columns) + ["abstained"]
    headers = ["problem", "variation"] + [short.get(c, c) for c in display_cols]

    rows: list[list[str]] = []
    for entry in matrix:
        cells = [
            str(entry.get("problem") or "?"),
            str(entry.get("variation") or "?"),
        ]
        for col in display_cols:
            cells.append(_fmt_cell(entry.get(col)))
        rows.append(cells)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [_line(headers), _line(["-" * w for w in widths])]
    lines.extend(_line(row) for row in rows)
    return "\n".join(lines)
