"""Agent-teacher provisioning drivers (Task D1).

Two shapes, per ``campaign/cast/subjects.py``:

* :func:`provision_seeded` — replays the existing filesystem-registry
  seeding scripts (subprocess, local DSN only) for an incumbent subject
  already authored under ``apollo/subjects/``.
* :func:`provision_authored` — the REAL WU-AAS teacher path: multipart PDF
  upload to ``POST /apollo/authored-sets``, poll ``GET
  .../authored-sets/{set_id}`` to a terminal status, then approve every
  problem the orchestrator held for review.

Both are pure request/flow logic over injected seams (a subprocess runner,
an httpx client, a sleep function) so they unit-test without Docker, a real
DB, or a real backend process. Only the default *real* implementations of
those seams — actually spawning a subprocess, actually sleeping — are
excluded from coverage (plan: "the live path pragma-excluded"); this module
is never invoked against a live stack from this task (that is Phase F).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from campaign.cast.subjects import SEEDED_SUBJECTS

_LOG = logging.getLogger(__name__)

RunSubprocess = Callable[[Sequence[str]], Awaitable[int]]
SleepFn = Callable[[float], Awaitable[None]]

_TERMINAL_STATUSES = frozenset({"done", "failed"})


class SeedProvisioningError(RuntimeError):
    """A seeding subprocess step exited non-zero."""


class AuthoredProvisioningError(RuntimeError):
    """The authored set finished (or a request failed) in a non-success state."""


class AuthoredProvisioningTimeout(AuthoredProvisioningError):
    """The authored set never reached a terminal status within the poll budget."""


# --- Seeded-incumbent provisioning --------------------------------------


@dataclass(frozen=True)
class SeedStepResult:
    command: tuple[str, ...]
    returncode: int


@dataclass(frozen=True)
class SeedProvisionResult:
    subject_key: str
    steps: tuple[SeedStepResult, ...]


async def _default_run_subprocess(cmd: Sequence[str]) -> int:  # pragma: no cover - thin subprocess passthrough
    proc = await asyncio.create_subprocess_exec(*cmd)
    return await proc.wait()


async def _run_step(runner: RunSubprocess, cmd: Sequence[str]) -> SeedStepResult:
    cmd_t = tuple(cmd)
    returncode = await runner(cmd_t)
    if returncode != 0:
        raise SeedProvisioningError(
            f"seed step failed (exit {returncode}): {' '.join(cmd_t)}"
        )
    return SeedStepResult(command=cmd_t, returncode=returncode)


async def provision_seeded(
    subject_key: str,
    dsn: str,
    *,
    run_subprocess: RunSubprocess | None = None,
    project_canon: bool = True,
) -> SeedProvisionResult:
    """Replay the filesystem-registry seeding scripts for one incumbent
    subject against ``dsn`` (a LOCAL campaign database URL).

    Runs, in order: ``seed_apollo_concept_registry`` (whole-registry walk;
    idempotent, safe to re-run per subject), ``seed_apollo_learner_model
    --subject-slug <slug>`` (Layer-1 KG rows for this subject only), and —
    unless ``project_canon=False`` — ``seed_canon_projection`` (rebuild
    ``:Canon`` in Neo4j from the just-seeded Postgres rows). Raises
    :class:`SeedProvisioningError` on the first non-zero exit; later steps
    are not attempted.
    """
    subject = SEEDED_SUBJECTS.get(subject_key)
    if subject is None:
        raise KeyError(
            f"unknown seeded subject: {subject_key!r} (known: {sorted(SEEDED_SUBJECTS)})"
        )
    runner = run_subprocess or _default_run_subprocess
    steps: list[SeedStepResult] = []

    registry_cmd: tuple[str, ...] = (
        sys.executable,
        "-m",
        "scripts.seed_apollo_concept_registry",
        "--database-url",
        dsn,
    )
    steps.append(await _run_step(runner, registry_cmd))

    learner_cmd: list[str] = [
        sys.executable,
        "-m",
        "scripts.seed_apollo_learner_model",
        "--database-url",
        dsn,
        "--subject-slug",
        subject.slug,
    ]
    if subject.concept_slug:
        learner_cmd += ["--concept-slug", subject.concept_slug]
    steps.append(await _run_step(runner, tuple(learner_cmd)))

    if project_canon:
        canon_cmd: tuple[str, ...] = (
            sys.executable,
            "-m",
            "scripts.seed_canon_projection",
            "--database-url",
            dsn,
        )
        steps.append(await _run_step(runner, canon_cmd))

    _LOG.info("provision_seeded_done subject=%s steps=%d", subject_key, len(steps))
    return SeedProvisionResult(subject_key=subject_key, steps=tuple(steps))


# --- WU-AAS authored provisioning (the REAL teacher path) ----------------


@dataclass(frozen=True)
class AuthoredProvisionResult:
    set_id: int
    status: str
    minted_problem_ids: tuple[int, ...]
    approved_problem_ids: tuple[int, ...]
    failed_approvals: tuple[tuple[int, str], ...]


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - thin asyncio passthrough
    await asyncio.sleep(seconds)


def _minted_and_held_ids(problems: list[dict]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    minted: list[int] = []
    held: list[int] = []
    for p in problems:
        if not isinstance(p, dict):
            continue
        pid = p.get("concept_problem_id")
        if pid is None:
            continue
        minted.append(int(pid))
        if p.get("review_required"):
            held.append(int(pid))
    return tuple(minted), tuple(held)


async def _poll_until_terminal(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    set_id: int,
    poll_interval: float,
    poll_timeout: float,
    sleep: SleepFn,
) -> dict:
    elapsed = 0.0
    while True:
        resp = await client.get(f"{base_url}/apollo/authored-sets/{set_id}", headers=headers)
        resp.raise_for_status()
        row = resp.json()
        if row.get("status") in _TERMINAL_STATUSES:
            return row
        if elapsed >= poll_timeout:
            raise AuthoredProvisioningTimeout(
                f"authored set {set_id} did not reach a terminal status within "
                f"{poll_timeout}s (last status={row.get('status')!r})"
            )
        await sleep(poll_interval)
        elapsed += poll_interval


async def provision_authored(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    teacher_token: str,
    search_space_id: int,
    problem_pdf: Path,
    solution_pdf: Path,
    poll_interval: float = 2.0,
    poll_timeout: float = 300.0,
    approve_reference: str = "ocr",
    sleep: SleepFn | None = None,
) -> AuthoredProvisionResult:
    """Drive the real teacher-facing WU-AAS upload → provision → approve flow.

    1. ``POST {base_url}/apollo/authored-sets`` with a multipart
       problem+solution PDF pair and ``search_space_id`` — mints the set and
       kicks off the backend's own background provisioning task.
    2. Poll ``GET {base_url}/apollo/authored-sets/{set_id}`` every
       ``poll_interval`` seconds until ``status`` is terminal (``done`` or
       ``failed``), or raise :class:`AuthoredProvisioningTimeout` after
       ``poll_timeout`` seconds.
    3. On ``status == "failed"`` raise :class:`AuthoredProvisioningError`
       with the recorded diagnostic.
    4. Approve every problem the orchestrator held for review
       (``review_required`` in its own ``result_summary["problems"]`` entry)
       via ``POST .../problems/{problem_id}/approve``.

    ``client`` is caller-owned (inject an ``httpx.AsyncClient`` wired to a
    ``MockTransport`` in tests; a real client against the campaign stack in
    Phase F). ``sleep`` defaults to real ``asyncio.sleep`` — inject a fake in
    tests so polling loops run instantly.
    """
    headers = {"Authorization": f"Bearer {teacher_token}"}
    sleep_fn = sleep or _default_sleep

    with problem_pdf.open("rb") as pf, solution_pdf.open("rb") as sf:
        files = {
            "problem": (problem_pdf.name, pf, "application/pdf"),
            "solution": (solution_pdf.name, sf, "application/pdf"),
        }
        data = {"search_space_id": str(search_space_id)}
        create_resp = await client.post(
            f"{base_url}/apollo/authored-sets", headers=headers, files=files, data=data
        )
    create_resp.raise_for_status()
    set_id = int(create_resp.json()["set_id"])

    status_row = await _poll_until_terminal(
        client,
        base_url=base_url,
        headers=headers,
        set_id=set_id,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        sleep=sleep_fn,
    )
    status = str(status_row.get("status"))
    if status != "done":
        diagnostic = (status_row.get("result_summary") or {}).get("error")
        raise AuthoredProvisioningError(
            f"authored set {set_id} finished with status={status!r}: {diagnostic}"
        )

    problems = (status_row.get("result_summary") or {}).get("problems") or []
    minted_ids, held_ids = _minted_and_held_ids(problems)

    approved: list[int] = []
    failed: list[tuple[int, str]] = []
    for problem_id in held_ids:
        approve_resp = await client.post(
            f"{base_url}/apollo/authored-sets/{set_id}/problems/{problem_id}/approve",
            headers=headers,
            json={"reference": approve_reference},
        )
        approve_resp.raise_for_status()
        body = approve_resp.json()
        if body.get("promoted"):
            approved.append(problem_id)
        else:
            failed.append((problem_id, str(body.get("diagnostic") or body.get("failed_gate"))))

    _LOG.info(
        "provision_authored_done set_id=%s minted=%d approved=%d failed=%d",
        set_id,
        len(minted_ids),
        len(approved),
        len(failed),
    )
    return AuthoredProvisionResult(
        set_id=set_id,
        status=status,
        minted_problem_ids=minted_ids,
        approved_problem_ids=tuple(approved),
        failed_approvals=tuple(failed),
    )
