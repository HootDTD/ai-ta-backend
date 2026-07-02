"""Unit tests for campaign.cast.teacher + campaign.cast.subjects.

Pure request/flow logic: the subprocess runner (provision_seeded) and the
httpx client + sleep function (provision_authored) are fully injected, so
none of this touches Docker, a real DB, or a real backend process. The
default *real* implementations of those seams are pragma-excluded (see
campaign/cast/teacher.py module docstring) and are exercised manually
against the local campaign stack in Phase F, not here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from campaign.cast import subjects, teacher

pytestmark = pytest.mark.unit


# --- subjects registry ----------------------------------------------------


def test_seeded_subjects_registry_has_both_incumbents():
    assert set(subjects.SEEDED_SUBJECTS) == {"fluid_mechanics", "macroeconomics"}
    assert subjects.SEEDED_SUBJECTS["fluid_mechanics"].slug == "fluid_mechanics"
    assert subjects.SEEDED_SUBJECTS["macroeconomics"].slug == "macroeconomics"


def test_authored_subjects_registry_has_new_and_held_out():
    assert "linear_motion" in subjects.AUTHORED_SUBJECTS
    assert subjects.AUTHORED_SUBJECTS["linear_motion"].held_out is False
    assert subjects.is_held_out("linear_motion") is False
    assert subjects.is_held_out("held_out_subject") is True
    assert subjects.is_held_out("fluid_mechanics") is False  # not an authored subject


def test_authored_subject_resolve_makes_paths_absolute(tmp_path):
    subject = subjects.LINEAR_MOTION.resolve(materials_dir=tmp_path)
    assert subject.problem_pdf == tmp_path / "linear_motion_problem.pdf"
    assert subject.solution_pdf == tmp_path / "linear_motion_solution.pdf"


def test_linear_motion_fixture_pdfs_exist_on_disk():
    subject = subjects.LINEAR_MOTION.resolve()
    assert subject.problem_pdf.is_file()
    assert subject.solution_pdf.is_file()
    assert subject.problem_pdf.read_bytes().startswith(b"%PDF")
    assert subject.solution_pdf.read_bytes().startswith(b"%PDF")


def test_materials_dir_points_at_cast_materials():
    assert subjects.materials_dir().name == "materials"
    assert subjects.materials_dir().is_dir()


def test_all_subject_keys_lists_seeded_then_authored():
    keys = subjects.all_subject_keys()
    assert keys == [
        "fluid_mechanics",
        "macroeconomics",
        "linear_motion",
        "held_out_subject",
    ]


# --- provision_seeded ------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_seeded_runs_registry_learner_and_canon_in_order():
    calls: list[tuple[str, ...]] = []

    async def fake_run(cmd):
        calls.append(tuple(cmd))
        return 0

    result = await teacher.provision_seeded(
        "fluid_mechanics", "postgresql://x", run_subprocess=fake_run
    )

    assert len(calls) == 3
    assert "scripts.seed_apollo_concept_registry" in calls[0]
    assert "scripts.seed_apollo_learner_model" in calls[1]
    assert "--subject-slug" in calls[1] and "fluid_mechanics" in calls[1]
    assert "scripts.seed_canon_projection" in calls[2]
    assert result.subject_key == "fluid_mechanics"
    assert [s.returncode for s in result.steps] == [0, 0, 0]


@pytest.mark.asyncio
async def test_provision_seeded_passes_concept_slug_when_set(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run(cmd):
        calls.append(tuple(cmd))
        return 0

    scoped = subjects.SeededSubject(
        key="fluid_mechanics", slug="fluid_mechanics", concept_slug="bernoulli_principle"
    )
    monkeypatch.setitem(teacher.SEEDED_SUBJECTS, "fluid_mechanics", scoped)

    await teacher.provision_seeded("fluid_mechanics", "postgresql://x", run_subprocess=fake_run)

    learner_cmd = calls[1]
    idx = learner_cmd.index("--concept-slug")
    assert learner_cmd[idx + 1] == "bernoulli_principle"


@pytest.mark.asyncio
async def test_provision_seeded_skips_canon_projection_when_disabled():
    calls: list[tuple[str, ...]] = []

    async def fake_run(cmd):
        calls.append(tuple(cmd))
        return 0

    result = await teacher.provision_seeded(
        "macroeconomics", "postgresql://x", run_subprocess=fake_run, project_canon=False
    )

    assert len(calls) == 2
    assert len(result.steps) == 2


@pytest.mark.asyncio
async def test_provision_seeded_unknown_subject_raises_keyerror():
    async def fake_run(cmd):
        return 0

    with pytest.raises(KeyError):
        await teacher.provision_seeded("not_a_subject", "postgresql://x", run_subprocess=fake_run)


@pytest.mark.asyncio
async def test_provision_seeded_stops_on_first_failing_step():
    calls: list[tuple[str, ...]] = []

    async def fake_run(cmd):
        calls.append(tuple(cmd))
        return 0 if len(calls) == 1 else 1

    with pytest.raises(teacher.SeedProvisioningError, match="seed step failed"):
        await teacher.provision_seeded("fluid_mechanics", "postgresql://x", run_subprocess=fake_run)

    assert len(calls) == 2  # third step (canon) never attempted


# --- provision_authored -----------------------------------------------------


def _pdf_pair(tmp_path):
    problem = tmp_path / "problem.pdf"
    solution = tmp_path / "solution.pdf"
    problem.write_bytes(b"%PDF-1.4 fake problem")
    solution.write_bytes(b"%PDF-1.4 fake solution")
    return problem, solution


async def _immediate_sleep(_seconds: float) -> None:
    return None


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_provision_authored_happy_path_no_held_problems(tmp_path):
    problem_pdf, solution_pdf = _pdf_pair(tmp_path)
    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok-123"
        if request.method == "POST" and request.url.path == "/apollo/authored-sets":
            return httpx.Response(200, json={"set_id": 7, "set_index": 1, "status": "pending"})
        if request.method == "GET" and request.url.path == "/apollo/authored-sets/7":
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                return httpx.Response(200, json={"set_id": 7, "status": "provisioning"})
            return httpx.Response(
                200,
                json={
                    "set_id": 7,
                    "status": "done",
                    "result_summary": {
                        "problems": [
                            {"concept_problem_id": 101, "review_required": False},
                            {"concept_problem_id": 102, "review_required": False},
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _mock_client(handler) as client:
        result = await teacher.provision_authored(
            client=client,
            base_url="http://backend.local",
            teacher_token="tok-123",
            search_space_id=5,
            problem_pdf=problem_pdf,
            solution_pdf=solution_pdf,
            poll_interval=0.0,
            sleep=_immediate_sleep,
        )

    assert result.set_id == 7
    assert result.status == "done"
    assert result.minted_problem_ids == (101, 102)
    assert result.approved_problem_ids == ()
    assert result.failed_approvals == ()
    assert poll_count["n"] == 2


@pytest.mark.asyncio
async def test_provision_authored_approves_held_problems(tmp_path):
    problem_pdf, solution_pdf = _pdf_pair(tmp_path)
    approve_calls: list[tuple[int, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/apollo/authored-sets":
            return httpx.Response(200, json={"set_id": 9, "status": "pending"})
        if request.method == "GET" and request.url.path == "/apollo/authored-sets/9":
            return httpx.Response(
                200,
                json={
                    "set_id": 9,
                    "status": "done",
                    "result_summary": {
                        "problems": [
                            {"concept_problem_id": 201, "review_required": True},
                            {"concept_problem_id": 202, "review_required": False},
                            {"review_required": True},  # no concept_problem_id: ignored
                            "not-a-dict",  # malformed entry: ignored
                        ]
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/apollo/authored-sets/9/problems/201/approve":
            body = json.loads(request.content)
            approve_calls.append((201, body))
            return httpx.Response(200, json={"promoted": True, "failed_gate": None, "diagnostic": ""})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _mock_client(handler) as client:
        result = await teacher.provision_authored(
            client=client,
            base_url="http://backend.local",
            teacher_token="tok-abc",
            search_space_id=3,
            problem_pdf=problem_pdf,
            solution_pdf=solution_pdf,
            poll_interval=0.0,
            sleep=_immediate_sleep,
        )

    assert result.minted_problem_ids == (201, 202)
    assert result.approved_problem_ids == (201,)
    assert result.failed_approvals == ()
    assert approve_calls == [(201, {"reference": "ocr"})]


@pytest.mark.asyncio
async def test_provision_authored_records_failed_approval(tmp_path):
    problem_pdf, solution_pdf = _pdf_pair(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/apollo/authored-sets":
            return httpx.Response(200, json={"set_id": 11, "status": "pending"})
        if request.method == "GET" and request.url.path == "/apollo/authored-sets/11":
            return httpx.Response(
                200,
                json={
                    "set_id": 11,
                    "status": "done",
                    "result_summary": {
                        "problems": [{"concept_problem_id": 301, "review_required": True}]
                    },
                },
            )
        if request.url.path == "/apollo/authored-sets/11/problems/301/approve":
            return httpx.Response(
                200, json={"promoted": False, "failed_gate": 2, "diagnostic": "solver mismatch"}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _mock_client(handler) as client:
        result = await teacher.provision_authored(
            client=client,
            base_url="http://backend.local",
            teacher_token="tok",
            search_space_id=1,
            problem_pdf=problem_pdf,
            solution_pdf=solution_pdf,
            poll_interval=0.0,
            sleep=_immediate_sleep,
        )

    assert result.approved_problem_ids == ()
    assert result.failed_approvals == ((301, "solver mismatch"),)


@pytest.mark.asyncio
async def test_provision_authored_raises_on_failed_status(tmp_path):
    problem_pdf, solution_pdf = _pdf_pair(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/apollo/authored-sets":
            return httpx.Response(200, json={"set_id": 13, "status": "pending"})
        if request.method == "GET" and request.url.path == "/apollo/authored-sets/13":
            return httpx.Response(
                200,
                json={
                    "set_id": 13,
                    "status": "failed",
                    "result_summary": {"error": "OCR extraction failed"},
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _mock_client(handler) as client:
        with pytest.raises(teacher.AuthoredProvisioningError, match="OCR extraction failed"):
            await teacher.provision_authored(
                client=client,
                base_url="http://backend.local",
                teacher_token="tok",
                search_space_id=1,
                problem_pdf=problem_pdf,
                solution_pdf=solution_pdf,
                poll_interval=0.0,
                sleep=_immediate_sleep,
            )


@pytest.mark.asyncio
async def test_provision_authored_times_out_when_never_terminal(tmp_path):
    problem_pdf, solution_pdf = _pdf_pair(tmp_path)
    sleep_calls: list[float] = []

    async def counting_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/apollo/authored-sets":
            return httpx.Response(200, json={"set_id": 21, "status": "pending"})
        if request.method == "GET" and request.url.path == "/apollo/authored-sets/21":
            return httpx.Response(200, json={"set_id": 21, "status": "provisioning"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _mock_client(handler) as client:
        with pytest.raises(teacher.AuthoredProvisioningTimeout):
            await teacher.provision_authored(
                client=client,
                base_url="http://backend.local",
                teacher_token="tok",
                search_space_id=1,
                problem_pdf=problem_pdf,
                solution_pdf=solution_pdf,
                poll_interval=1.0,
                poll_timeout=2.0,
                sleep=counting_sleep,
            )

    assert len(sleep_calls) >= 2
