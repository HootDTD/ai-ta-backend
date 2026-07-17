"""DB-free coverage for the live synchronous authored-problem seam."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import apollo.provisioning.authored_problem as subject
from apollo.provisioning.metered_chat import CostBudgetExceeded
from apollo.provisioning.promote import PromoteResult
from apollo.provisioning.solution import SolutionDraftError
from apollo.provisioning.tag_mint import TagMintError


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


def _inputs():
    return {
        "db": AsyncMock(),
        "neo": object(),
        "authored": SimpleNamespace(problem_code="authored.hash"),
        "search_space_id": 7,
        "ingest_concept_id": 11,
        "construct_chat_fn": lambda *_a, **_k: "{}",
        "judge_fn": lambda *_a, **_k: "{}",
        "tag_chat_fn": lambda _prompt: "{}",
        "embed_fn": lambda _text: [1.0],
    }


def _happy_until_mint(monkeypatch):
    monkeypatch.setattr(subject, "construct_authored_reference", AsyncMock(return_value=object()))
    monkeypatch.setattr(subject, "validate_pair", AsyncMock(return_value=object()))
    monkeypatch.setattr(subject, "rejection_from_verdict", lambda _verdict: None)
    monkeypatch.setattr(
        subject,
        "build_authored_approved_pair",
        lambda *_a, **_k: SimpleNamespace(problem={"id": "p"}),
    )


@pytest.mark.asyncio
async def test_construct_failure_is_clean_reject(monkeypatch):
    monkeypatch.setattr(
        subject,
        "construct_authored_reference",
        AsyncMock(side_effect=SolutionDraftError("bad draft")),
    )
    result = await subject.provision_authored_problem(**_inputs())
    assert (result.outcome, result.stage, result.diagnostic) == (
        "rejected",
        "construct",
        "bad draft",
    )


@pytest.mark.asyncio
async def test_pairing_failure_is_clean_reject(monkeypatch):
    _happy_until_mint(monkeypatch)
    monkeypatch.setattr(
        subject,
        "rejection_from_verdict",
        lambda _verdict: SimpleNamespace(diagnostic="not faithful"),
    )
    result = await subject.provision_authored_problem(**_inputs())
    assert (result.outcome, result.stage, result.diagnostic) == (
        "rejected",
        "pairing_gate",
        "not faithful",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TagMintError("bad tag"), CostBudgetExceeded(tokens=10, ceiling=5)]
)
async def test_mint_failure_is_clean_reject(monkeypatch, error):
    _happy_until_mint(monkeypatch)
    monkeypatch.setattr(subject, "tag_and_mint", AsyncMock(side_effect=error))
    result = await subject.provision_authored_problem(**_inputs())
    assert result.outcome == "rejected"
    assert result.stage == "tag_mint"


@pytest.mark.asyncio
async def test_missing_inventory_row_raises(monkeypatch):
    _happy_until_mint(monkeypatch)
    monkeypatch.setattr(
        subject, "tag_and_mint", AsyncMock(return_value=SimpleNamespace(concept_id=12))
    )
    monkeypatch.setattr(subject, "_find_tier1_row_id", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="has no Tier-1 row"):
        await subject.provision_authored_problem(**_inputs())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("promote_result", "expected"),
    [
        (PromoteResult(promoted=True), ("promoted", "ok", None)),
        (PromoteResult(promoted=False, failed_gate=4, diagnostic="lint"),
         ("rejected", "promotion_lint", 4)),
    ],
)
async def test_promotion_outcomes(monkeypatch, promote_result, expected):
    _happy_until_mint(monkeypatch)
    monkeypatch.setattr(
        subject, "tag_and_mint", AsyncMock(return_value=SimpleNamespace(concept_id=12))
    )
    monkeypatch.setattr(subject, "_find_tier1_row_id", AsyncMock(return_value=99))
    monkeypatch.setattr(subject, "_concept_dup_hashes", AsyncMock(return_value={"old"}))
    promote = AsyncMock(return_value=promote_result)
    monkeypatch.setattr(subject, "promote", promote)

    result = await subject.provision_authored_problem(**_inputs())
    assert (result.outcome, result.stage, result.failed_gate) == expected
    assert promote.await_args.kwargs["concept_problem_id"] == 99
    assert promote.await_args.kwargs["existing_problem_hashes"] == {"old"}


@pytest.mark.asyncio
async def test_inventory_lookup_and_duplicate_hash_helpers(monkeypatch):
    db = AsyncMock()
    db.execute.side_effect = [_ScalarResult(44), _RowsResult([{"ok": True}, {"bad": True}])]
    assert await subject._find_tier1_row_id(
        db, concept_id=11, problem_code="authored.hash"
    ) == 44

    model_validate = lambda payload: payload if "ok" in payload else (_ for _ in ()).throw(
        ValueError("bad")
    )
    monkeypatch.setattr(subject.Problem, "model_validate", model_validate)
    monkeypatch.setattr(subject, "problem_dup_hash", lambda problem: f"hash:{problem['ok']}")
    assert await subject._concept_dup_hashes(db, concept_id=12) == {"hash:True"}


@pytest.mark.asyncio
async def test_authored_chunk_loader_and_empty_enrichment():
    import apollo.provisioning.authored_sets.api as authored_api
    import apollo.provisioning.authored_sets.orchestrator as authored_orchestrator

    row = SimpleNamespace(
        id=1,
        content="question",
        document_id=9,
        page_number=2,
        section_path="Exercises",
        chunk_type="text",
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(all=lambda: [row])
    chunks = await authored_orchestrator._load_chunks(db, document_id=9)
    assert [
        chunks[0].id,
        chunks[0].content,
        chunks[0].document_id,
        chunks[0].page_number,
        chunks[0].section_path,
        chunks[0].chunk_type,
    ] == [1, "question", 9, 2, "Exercises", "text"]

    assert await authored_api._enrich_problem_reviews(
        db, [None], ingest_run_id=123
    ) == [None]
