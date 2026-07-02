"""Cross-check authored persona files against REAL subject data (Task D2).

Every ``expected.credited``/``expected.unresolved`` key a persona brief
declares must be a real ``entity_key`` in that subject/concept/problem's
``reference_solution`` (``apollo/subjects/<subject>/concepts/<concept>/
problems/<problem_id>.json``), and every ``expected.misconceptions`` key must
be a real ``misc.*`` key in that concept's ``misconceptions.json``. This
module loads the actual on-disk subject data — it never hand-mints a
canonical-key list — so an authoring typo (or drift when a subject's
problem JSON changes) fails loudly instead of silently poisoning the S3/S4
stage audits (``campaign/judges/``).

``linear_motion`` is the WU-AAS-authored campaign subject (plan D1/D2): its
real canonical keys don't exist until the actual teacher-upload path mints
them from the ingested PDF (Task F2), so there is no
``apollo/subjects/linear_motion/`` tree to check against yet. Its persona
briefs are authored against a hand-written PROVISIONAL reference graph
(``campaign/cast/personas/linear_motion/reference/``) that mirrors the exact
worked solution in the fixture PDF (``campaign/cast/materials/
generate_fixtures.py``) so at least the arithmetic and canonical-key
*convention* are grounded and testable now. When F2 mints the real set, the
provisional reference (and these persona files) must be reconciled against
whatever canonical keys the real parse produces — flagged in
``campaign/README.md`` as a known follow-up, not silently assumed correct.
"""

from __future__ import annotations

import json
from pathlib import Path

from campaign.cast.personas.schema import PersonaAttempt

__all__ = [
    "PERSONAS_DIR",
    "REPO_ROOT",
    "PROVISIONAL_SUBJECTS",
    "iter_persona_files",
    "load_persona_file",
    "reference_keys_for",
    "misconception_keys_for",
    "validate_persona",
    "validate_all",
]

PERSONAS_DIR = Path(__file__).resolve().parent
REPO_ROOT = PERSONAS_DIR.parents[2]
_SUBJECTS_ROOT = REPO_ROOT / "apollo" / "subjects"

#: Subjects with no real ``apollo/subjects/`` tree yet (WU-AAS, pre-mint).
#: Their persona files are checked against a hand-authored provisional
#: reference under ``campaign/cast/personas/<subject>/reference/`` instead.
PROVISIONAL_SUBJECTS: frozenset[str] = frozenset({"linear_motion"})


def _concept_dir(subject: str, concept: str) -> Path:
    if subject in PROVISIONAL_SUBJECTS:
        return PERSONAS_DIR / subject / "reference" / concept
    return _SUBJECTS_ROOT / subject / "concepts" / concept


def iter_persona_files(base: Path | None = None) -> list[Path]:
    """Every authored persona JSON file. Excludes provisional-reference data
    (``<subject>/reference/**``) and any file whose stem starts with ``_``
    (reserved for non-persona fixtures)."""
    root = base or PERSONAS_DIR
    files: list[Path] = []
    for path in sorted(root.glob("*/*.json")):
        if path.stem.startswith("_"):
            continue
        files.append(path)
    return files


def load_persona_file(path: Path) -> PersonaAttempt:
    data = json.loads(path.read_text(encoding="utf-8"))
    return PersonaAttempt.model_validate(data)


def _load_problem_data(subject: str, concept: str, problem_id: str) -> dict:
    """Locate a problem JSON by its internal ``id`` field (the real
    problem identifier the runtime/API uses), NOT by filename — problem
    files are named positionally (``problem_01.json``, ``problem_02.json``,
    ...) while ``PersonaAttempt.problem_id`` carries the semantic id
    (e.g. ``bernoulli_horizontal_pipe_find_p2``)."""
    problems_dir = _concept_dir(subject, concept) / "problems"
    if not problems_dir.is_dir():
        raise FileNotFoundError(
            f"no problems/ dir for subject={subject!r} concept={concept!r} "
            f"(looked at {problems_dir})"
        )
    for path in sorted(problems_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("id") == problem_id:
            return data
    raise FileNotFoundError(
        f"no problem with id={problem_id!r} under {problems_dir} "
        f"(subject={subject!r} concept={concept!r})"
    )


def reference_keys_for(subject: str, concept: str, problem_id: str) -> set[str]:
    """Real (or provisional, for WU-AAS subjects) ``entity_key`` set for one
    problem's ``reference_solution``."""
    data = _load_problem_data(subject, concept, problem_id)
    return {step["entity_key"] for step in data["reference_solution"]}


def misconception_keys_for(subject: str, concept: str) -> set[str]:
    """Real (or provisional) ``misc.*`` key set for one concept."""
    misc_path = _concept_dir(subject, concept) / "misconceptions.json"
    if not misc_path.exists():
        raise FileNotFoundError(
            f"no misconceptions.json for subject={subject!r} concept={concept!r} "
            f"(looked at {misc_path})"
        )
    data = json.loads(misc_path.read_text(encoding="utf-8"))
    return {entry["key"] for entry in data["misconceptions"]}


def validate_persona(persona: PersonaAttempt) -> list[str]:
    """Return a list of human-readable error strings (empty == valid)."""
    errors: list[str] = []
    try:
        ref_keys = reference_keys_for(persona.subject, persona.concept, persona.problem_id)
    except FileNotFoundError as exc:
        return [str(exc)]

    unknown_credited = set(persona.expected.credited) - ref_keys
    unknown_unresolved = set(persona.expected.unresolved) - ref_keys
    if unknown_credited:
        errors.append(f"expected.credited has unknown keys: {sorted(unknown_credited)}")
    if unknown_unresolved:
        errors.append(f"expected.unresolved has unknown keys: {sorted(unknown_unresolved)}")

    if persona.expected.misconceptions:
        try:
            misc_keys = misconception_keys_for(persona.subject, persona.concept)
        except FileNotFoundError as exc:
            errors.append(str(exc))
        else:
            unknown_misc = set(persona.expected.misconceptions) - misc_keys
            if unknown_misc:
                errors.append(f"expected.misconceptions has unknown keys: {sorted(unknown_misc)}")
    return errors


def validate_all(base: Path | None = None) -> dict[Path, list[str]]:
    """Validate every authored persona file. Returns ``{path: [errors]}`` for
    files that failed (empty dict == the whole corpus is clean)."""
    failures: dict[Path, list[str]] = {}
    for path in iter_persona_files(base):
        try:
            persona = load_persona_file(path)
        except Exception as exc:  # pydantic ValidationError, json errors, etc.
            failures[path] = [f"failed to load: {exc}"]
            continue
        errors = validate_persona(persona)
        if errors:
            failures[path] = errors
    return failures
