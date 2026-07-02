"""Campaign subject registry (Task D1).

Four campaign subjects per the plan (Phase D preamble): two **seeded
incumbents** already on disk under ``apollo/subjects/`` (fluid_mechanics,
macroeconomics), one **WU-AAS-authored** subject minted live through the
real teacher upload path, and one **held-out** subject (same authored path,
provisioned only during the gate phase — Task F2, not here).

This module is pure data + tiny path-resolution helpers; no I/O, no DB, no
HTTP. The actual provisioning verbs live in ``campaign/cast/teacher.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_MATERIALS_DIR = Path(__file__).resolve().parent / "materials"


@dataclass(frozen=True)
class SeededSubject:
    """An incumbent subject already authored on disk under ``apollo/subjects/``.

    Provisioned by replaying the filesystem-registry seeding scripts
    (``scripts/seed_apollo_concept_registry.py`` +
    ``scripts/seed_apollo_learner_model.py`` [+ optionally
    ``scripts/seed_canon_projection.py``]) against the campaign's local DB.
    """

    key: str
    slug: str
    concept_slug: str | None = None  # None == every concept under the subject


@dataclass(frozen=True)
class AuthoredSubject:
    """A subject minted through the REAL WU-AAS teacher upload path.

    ``problem_pdf`` / ``solution_pdf`` are paths (relative to
    ``campaign/cast/materials/``) to the source PDF pair a teacher uploads.
    ``held_out`` marks a subject that Task F1's tune-phase orchestrator must
    refuse to provision (Phase F rule: held-out is gate-phase-only).
    """

    key: str
    problem_pdf: Path
    solution_pdf: Path
    held_out: bool = False

    def resolve(self, *, materials_dir: Path | None = None) -> AuthoredSubject:
        """Return a copy with both PDF paths made absolute under
        ``materials_dir`` (default: ``campaign/cast/materials/``)."""
        base = materials_dir or _MATERIALS_DIR
        return AuthoredSubject(
            key=self.key,
            problem_pdf=base / self.problem_pdf,
            solution_pdf=base / self.solution_pdf,
            held_out=self.held_out,
        )


# --- Seeded incumbents -------------------------------------------------

FLUID_MECHANICS = SeededSubject(key="fluid_mechanics", slug="fluid_mechanics")
MACROECONOMICS = SeededSubject(key="macroeconomics", slug="macroeconomics")

SEEDED_SUBJECTS: dict[str, SeededSubject] = {s.key: s for s in (FLUID_MECHANICS, MACROECONOMICS)}

# --- WU-AAS authored subjects -------------------------------------------
#
# ``linear_motion`` is the new subject this task adds a tiny fixture PDF
# pair for (plan D1: "≥1 new subject"). The held-out subject's PDF pair is
# generated on demand by Task F2 (it must not exist before the gate phase);
# its registry entry is a placeholder pointing at a path that does not need
# to exist until then.

LINEAR_MOTION = AuthoredSubject(
    key="linear_motion",
    problem_pdf=Path("linear_motion_problem.pdf"),
    solution_pdf=Path("linear_motion_solution.pdf"),
    held_out=False,
)

HELD_OUT_PLACEHOLDER = AuthoredSubject(
    key="held_out_subject",
    problem_pdf=Path("held_out_problem.pdf"),
    solution_pdf=Path("held_out_solution.pdf"),
    held_out=True,
)

AUTHORED_SUBJECTS: dict[str, AuthoredSubject] = {
    s.key: s for s in (LINEAR_MOTION, HELD_OUT_PLACEHOLDER)
}


def materials_dir() -> Path:
    return _MATERIALS_DIR


def all_subject_keys() -> list[str]:
    """Every registered subject key, seeded first (deterministic order)."""
    return [*SEEDED_SUBJECTS, *AUTHORED_SUBJECTS]


def is_held_out(subject_key: str) -> bool:
    subject = AUTHORED_SUBJECTS.get(subject_key)
    return subject is not None and subject.held_out
