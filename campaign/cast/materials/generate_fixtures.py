"""Tiny PDF-pair generator for WU-AAS campaign fixtures (Task D1).

The real WU-AAS path (``campaign.cast.teacher.provision_authored``) uploads
two PDFs — a problem set and its worked solution — through
``POST /apollo/authored-sets``. The ingestion pipeline (PyMuPDF text
extraction) needs genuine PDF bytes, not placeholder text files, so this
module builds minimal single-page PDFs with :mod:`pymupdf` (``fitz``,
already a pinned dependency — see ``requirements.txt``).

Run as a script to (re)generate the checked-in fixtures under
``campaign/cast/materials/``:

    python -m campaign.cast.materials.generate_fixtures

The generated files are small (single page, plain text) and are committed
to the repo — regenerating is only needed if the fixture content changes.
"""

from __future__ import annotations

from pathlib import Path

_MATERIALS_DIR = Path(__file__).resolve().parent

#: One new WU-AAS subject's source material (plan D1: "≥1 new subject").
#: Deliberately tiny — a single worked kinematics problem — so ingestion +
#: provisioning runs fast in the campaign.
LINEAR_MOTION_PROBLEM_TEXT = """Linear Motion — Practice Problem Set 1

Problem 1.
A cyclist accelerates from rest at a constant rate of 2.0 m/s^2 for 5.0
seconds along a straight road.

(a) What is the cyclist's velocity at t = 5.0 s?
(b) How far does the cyclist travel during this time?
"""

LINEAR_MOTION_SOLUTION_TEXT = """Linear Motion — Practice Problem Set 1 (Solution)

Problem 1.
Given: initial velocity v0 = 0 m/s, acceleration a = 2.0 m/s^2, time t = 5.0 s.

(a) v = v0 + a*t = 0 + (2.0)(5.0) = 10.0 m/s.

(b) x = v0*t + (1/2)*a*t^2 = 0 + 0.5*(2.0)*(5.0)^2 = 25.0 m.

The cyclist reaches 10.0 m/s and travels 25.0 m.
"""


def build_fixture_pdf(text: str, path: Path) -> Path:
    """Write a minimal single-page PDF containing ``text`` to ``path``.

    Uses PyMuPDF's in-memory document builder — no external binaries, no
    fonts to bundle (the base14 ``helv`` font is built into the PDF spec).
    """
    import fitz  # local import: keeps this module importable without the

    # optional PDF dependency for callers that only need the text constants.
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontname="helv", fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def generate_linear_motion_fixtures(out_dir: Path | None = None) -> tuple[Path, Path]:
    """Write the linear_motion problem+solution PDF pair; return their paths."""
    base = out_dir or _MATERIALS_DIR
    problem_path = build_fixture_pdf(
        LINEAR_MOTION_PROBLEM_TEXT, base / "linear_motion_problem.pdf"
    )
    solution_path = build_fixture_pdf(
        LINEAR_MOTION_SOLUTION_TEXT, base / "linear_motion_solution.pdf"
    )
    return problem_path, solution_path


if __name__ == "__main__":  # pragma: no cover - manual regeneration entrypoint
    problem, solution = generate_linear_motion_fixtures()
    print(f"wrote {problem}")
    print(f"wrote {solution}")
