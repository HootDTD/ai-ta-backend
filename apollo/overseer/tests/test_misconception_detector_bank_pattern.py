"""RED tests for the misconception-detector bank_pattern tier (T4).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.3, amended by A2 (real personas only — not used directly here; this
module is a pure/DB-fixture unit, no persona fixtures needed).

``detect_bank_pattern`` embeds each raw student utterance via the injected
``EmbedFn`` and compares against the concept's misconception bank. On
Postgres it would delegate to ``misconception_bank.match_by_embedding``
(pgvector-only per that module's own docstring); on SQLite (this test suite)
it falls back to an in-memory cosine computed directly against the supplied
``bank_entries`` tuple — so these tests exercise the offline path with a
stub ``EmbedFn`` and zero network/pgvector dependency.
"""
from __future__ import annotations

import math

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.bank_pattern import detect_bank_pattern
from apollo.overseer.misconception_detector.config import BANK_SIM_FLOOR
from apollo.overseer.misconception_detector.types import ConceptFinding
from apollo.persistence.models import Concept, Misconception, Subject
from database.models import Base


@pytest_asyncio.fixture
async def sqlite_db():
    """Bare in-memory SQLite session. bank_pattern's SQLite path never
    issues a real query against apollo_misconceptions — it works purely off
    the injected ``bank_entries`` tuple — but we still hand it a real
    AsyncSession to match the production signature (and to exercise the
    dialect-detection branch against a real sqlite bind). Only a small
    subset of tables is created (mirrors test_misconception_bank.py) since
    the full metadata includes JSONB columns SQLite can't compile."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Subject.__table__,
        Concept.__table__,
        Misconception.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


def _entry(code: str = "includes_transfers", embedding: tuple[float, float] = (1.0, 0.0)) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=1,
        concept_id=42,
        code=code,
        description="GDP includes transfer payments",
        confusion_pair=None,
        trigger_phrases=(),
        probe_question="Are transfers part of GDP?",
        rt_steps=(),
    )


def _unit_vec(angle_deg: float) -> list[float]:
    """A 2D unit vector at the given angle, used as a fake embedding so
    cosine similarity is exactly controllable in tests."""
    rad = math.radians(angle_deg)
    return [math.cos(rad), math.sin(rad)]


class _StubEmbedFn:
    """DI stub satisfying the EmbedFn Protocol — maps a fixed utterance
    string to a pre-baked vector, with no network/OpenAI call."""

    def __init__(self, mapping: dict[str, list[float]]):
        self._mapping = mapping

    def __call__(self, text: str) -> list[float]:
        return self._mapping[text]


@pytest.mark.asyncio
class TestDetectBankPattern:
    async def test_utterance_within_floor_yields_misconception_finding(self, sqlite_db):
        # Bank entry embedding at 0deg; utterance embedding at a small angle
        # so cosine similarity is comfortably >= BANK_SIM_FLOOR (0.80).
        bank_vec = _unit_vec(0.0)
        utterance = "GDP includes government transfer payments to households"
        # ~11.5 degrees apart -> cosine ~0.98
        utt_vec = _unit_vec(10.0)
        embed_fn = _StubEmbedFn({utterance: utt_vec})

        entry = MisconceptionEntry(
            id=7,
            concept_id=42,
            code="includes_transfers",
            description="GDP includes transfer payments",
            confusion_pair=None,
            trigger_phrases=(),
            probe_question="Are transfers part of GDP?",
            rt_steps=(),
        )

        # Monkeypatch-free: inject the bank entry's embedding via a helper
        # the implementation is expected to expose, OR (more likely) the
        # implementation computes cosine against embed_fn(entry.description).
        # We control this by also registering the description's embedding.
        embed_fn._mapping[entry.description] = bank_vec

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        finding = findings[0]
        assert isinstance(finding, ConceptFinding)
        assert finding.verdict == "misconception"
        assert finding.source == "bank_pattern"
        assert finding.signature == "misc.includes_transfers"
        assert finding.corroborated is False
        assert finding.evidence_span == utterance
        assert finding.confidence == pytest.approx(_cosine(utt_vec, bank_vec), abs=1e-6)
        assert finding.confidence >= BANK_SIM_FLOOR
        assert finding.bank_match_above_floor is True
        assert finding.bank_code == "includes_transfers"

    async def test_utterance_below_floor_yields_no_finding(self, sqlite_db):
        """Non-positive-similarity best match (opposite-direction embedding,
        cosine -1.0) still abstains (A10: the below-floor emission requires
        similarity STRICTLY > 0.0, not merely < floor). NOTE: this must NOT
        assert the old below-floor-abstains behavior for a POSITIVE
        below-floor similarity — see TestBelowFloorEmission for that (now
        changed) case."""
        bank_vec = _unit_vec(0.0)
        utterance = "I think dogs are great pets"
        # 180 degrees apart -> cosine exactly -1.0 (unambiguously <= 0, no
        # floating-point-near-zero ambiguity the way an exact 90deg would be).
        utt_vec = _unit_vec(180.0)
        entry = _entry()
        embed_fn = _StubEmbedFn({utterance: utt_vec, entry.description: bank_vec})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_empty_bank_yields_no_finding(self, sqlite_db):
        utterance = "GDP includes transfer payments"
        embed_fn = _StubEmbedFn({utterance: _unit_vec(0.0)})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(),
        )

        assert findings == ()

    async def test_empty_utterances_yields_no_finding(self, sqlite_db):
        entry = _entry()
        embed_fn = _StubEmbedFn({entry.description: _unit_vec(0.0)})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_multiple_utterances_only_hit_ones_yield_findings(self, sqlite_db):
        bank_vec = _unit_vec(0.0)
        hit_utterance = "GDP includes transfer payments to households"
        miss_utterance = "The weather today is sunny"
        embed_fn = _StubEmbedFn({
            hit_utterance: _unit_vec(5.0),
            # 180deg apart -> cosine exactly -1.0, unambiguously <= 0 (A10's
            # `> 0.0` guard) unlike an exact-90deg vector which floating-point
            # cosine computes as a tiny POSITIVE residual, not exactly 0.
            miss_utterance: _unit_vec(180.0),
        })
        entry = _entry()
        embed_fn._mapping[entry.description] = bank_vec

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(miss_utterance, hit_utterance),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        assert findings[0].evidence_span == hit_utterance

    async def test_multiple_bank_entries_best_match_wins_per_utterance(self, sqlite_db):
        utterance = "GDP includes transfer payments"
        close_entry = MisconceptionEntry(
            id=1,
            concept_id=42,
            code="close_match",
            description="close description",
            confusion_pair=None,
            trigger_phrases=(),
            probe_question="q1",
            rt_steps=(),
        )
        far_entry = MisconceptionEntry(
            id=2,
            concept_id=42,
            code="far_match",
            description="far description",
            confusion_pair=None,
            trigger_phrases=(),
            probe_question="q2",
            rt_steps=(),
        )
        embed_fn = _StubEmbedFn({
            utterance: _unit_vec(2.0),
            close_entry.description: _unit_vec(0.0),
            far_entry.description: _unit_vec(60.0),
        })

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(far_entry, close_entry),
        )

        assert len(findings) == 1
        assert findings[0].signature == "misc.close_match"

    async def test_concept_id_none_still_works_offline(self, sqlite_db):
        """concept_id is only used for the (bypassed-on-SQLite) Postgres
        query path; the SQLite/in-memory cosine path must work even when
        concept_id is None, since the caller supplies bank_entries directly."""
        bank_vec = _unit_vec(0.0)
        utterance = "GDP includes transfer payments"
        entry = _entry()
        embed_fn = _StubEmbedFn({utterance: _unit_vec(5.0), entry.description: bank_vec})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=None,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1

    async def test_embed_fn_raising_soft_fails_to_no_finding(self, sqlite_db):
        """A detector error must never break grading — an embed_fn that
        raises for a given utterance is swallowed and simply yields no
        finding for that utterance (soft-fail), not an exception."""

        class _RaisingEmbedFn:
            def __call__(self, text: str) -> list[float]:
                raise RuntimeError("embedding service unavailable")

        entry = _entry()
        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=("GDP includes transfer payments",),
            embed_fn=_RaisingEmbedFn(),
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_bank_entry_embed_failure_is_skipped_not_raised(self, sqlite_db):
        """One bank entry's description fails to embed (e.g. a transient
        embedding-service hiccup for that specific string) — the other
        bank entries must still be considered, not the whole call aborted."""

        good_entry = _entry(code="good_entry")
        bad_entry = MisconceptionEntry(
            id=99,
            concept_id=42,
            code="bad_entry",
            description="__RAISE__",
            confusion_pair=None,
            trigger_phrases=(),
            probe_question="q",
            rt_steps=(),
        )
        utterance = "GDP includes transfer payments"

        class _PartialRaisingEmbedFn:
            def __call__(self, text: str) -> list[float]:
                if text == "__RAISE__":
                    raise RuntimeError("bad embed")
                if text == utterance:
                    return _unit_vec(5.0)
                if text == good_entry.description:
                    return _unit_vec(0.0)
                raise AssertionError(f"unexpected embed call: {text!r}")

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=_PartialRaisingEmbedFn(),
            bank_entries=(bad_entry, good_entry),
        )

        assert len(findings) == 1
        assert findings[0].signature == "misc.good_entry"

    async def test_all_bank_entries_fail_to_embed_yields_no_finding(self, sqlite_db):
        """Every bank entry's description fails to embed -> the fallback
        must abstain cleanly rather than raise or crash."""

        bad_entry = MisconceptionEntry(
            id=1,
            concept_id=42,
            code="bad_entry",
            description="__RAISE__",
            confusion_pair=None,
            trigger_phrases=(),
            probe_question="q",
            rt_steps=(),
        )
        utterance = "GDP includes transfer payments"

        class _BankOnlyRaisingEmbedFn:
            def __call__(self, text: str) -> list[float]:
                if text == "__RAISE__":
                    raise RuntimeError("bad embed")
                return _unit_vec(0.0)

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=_BankOnlyRaisingEmbedFn(),
            bank_entries=(bad_entry,),
        )

        assert findings == ()

    async def test_multiple_utterances_first_dialect_lookup_reused(self, sqlite_db):
        """Two utterances against a single bank entry — exercises the
        best-match loop with more than one candidate iteration per call so
        the 'first candidate becomes best' branch and the 'later candidate
        does not beat it' branch both run within one bank_entries tuple."""

        entry_a = _entry(code="entry_a")
        entry_b = MisconceptionEntry(
            id=2,
            concept_id=42,
            code="entry_b",
            description="entry b description",
            confusion_pair=None,
            trigger_phrases=(),
            probe_question="q",
            rt_steps=(),
        )
        utterance = "GDP includes transfer payments"
        embed_fn = _StubEmbedFn({
            utterance: _unit_vec(1.0),
            entry_a.description: _unit_vec(0.0),
            entry_b.description: _unit_vec(0.0),
        })

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry_a, entry_b),
        )

        assert len(findings) == 1


@pytest.mark.asyncio
class TestBelowFloorEmission:
    """RED tests for A10 (corroboration/keying redesign spec §4.5, §7):
    ``detect_bank_pattern`` now emits its best-ranked match even below
    ``BANK_SIM_FLOOR``, tagged ``bank_match_above_floor=False`` — a
    corroboration-only finding the gate may use to co-key a judge (never as
    a standalone dock)."""

    async def test_above_floor_match_tagged_true(self, sqlite_db):
        bank_vec = _unit_vec(0.0)
        utterance = "GDP includes government transfer payments to households"
        utt_vec = _unit_vec(10.0)  # cosine ~0.98, above floor
        entry = _entry()
        embed_fn = _StubEmbedFn({utterance: utt_vec, entry.description: bank_vec})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        assert findings[0].bank_match_above_floor is True
        assert findings[0].bank_code == "includes_transfers"
        assert findings[0].signature == "misc.includes_transfers"

    async def test_below_floor_best_match_emitted_tagged_false(self, sqlite_db):
        """A best match with 0 < similarity < BANK_SIM_FLOOR is EMITTED
        (not abstained), tagged bank_match_above_floor=False."""
        bank_vec = _unit_vec(0.0)
        utterance = "somewhat related utterance"
        # ~60 degrees apart -> cosine 0.5, below BANK_SIM_FLOOR (0.80) but > 0.
        utt_vec = _unit_vec(60.0)
        entry = _entry()
        embed_fn = _StubEmbedFn({utterance: utt_vec, entry.description: bank_vec})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        finding = findings[0]
        assert finding.bank_match_above_floor is False
        assert finding.bank_code == "includes_transfers"
        assert finding.signature == "misc.includes_transfers"
        assert finding.confidence == pytest.approx(_cosine(utt_vec, bank_vec), abs=1e-6)
        assert finding.confidence < BANK_SIM_FLOOR

    async def test_no_bank_still_abstains(self, sqlite_db):
        utterance = "GDP includes transfer payments"
        embed_fn = _StubEmbedFn({utterance: _unit_vec(0.0)})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(),
        )

        assert findings == ()

    async def test_embed_failure_abstains(self, sqlite_db):
        class _RaisingEmbedFn:
            def __call__(self, text: str) -> list[float]:
                raise RuntimeError("embedding service unavailable")

        entry = _entry()
        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=("GDP includes transfer payments",),
            embed_fn=_RaisingEmbedFn(),
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_zero_similarity_abstains(self, sqlite_db):
        """A best match with similarity <= 0 must NOT emit a junk
        corroboration-only row — the `> 0.0` guard. Uses exact axis-aligned
        integer vectors (not trig-derived) so the cosine is EXACTLY 0.0, with
        no floating-point residual."""
        bank_vec = [1.0, 0.0]
        utterance = "completely unrelated"
        utt_vec = [0.0, 1.0]  # exactly orthogonal -> cosine exactly 0.0
        entry = _entry()
        embed_fn = _StubEmbedFn({utterance: utt_vec, entry.description: bank_vec})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_negative_similarity_abstains(self, sqlite_db):
        bank_vec = _unit_vec(0.0)
        utterance = "opposite direction utterance"
        utt_vec = _unit_vec(180.0)  # cosine -1.0
        entry = _entry()
        embed_fn = _StubEmbedFn({utterance: utt_vec, entry.description: bank_vec})

        findings = await detect_bank_pattern(
            sqlite_db,
            concept_id=42,
            student_utterances=(utterance,),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_postgres_path_below_floor_now_emits_tagged_false(self, monkeypatch):
        """The postgres/pgvector dispatch path also honors the new
        ``above_floor`` tag: a below-floor best match from
        ``match_by_embedding`` is now emitted (not abstained), tagged False."""
        entry = _entry()

        async def _fake_match_by_embedding(db, *, concept_id, query_embedding, k=3):
            return [(entry, 0.55)]

        monkeypatch.setattr(
            "apollo.overseer.misconception_bank.match_by_embedding",
            _fake_match_by_embedding,
        )

        embed_fn = _StubEmbedFn({"some utterance": _unit_vec(0.0)})
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=42,
            student_utterances=("some utterance",),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        assert findings[0].bank_match_above_floor is False
        assert findings[0].confidence == pytest.approx(0.55)


class _FakeDialect:
    def __init__(self, name: str):
        self.name = name


class _FakeBind:
    def __init__(self, dialect_name: str):
        self.dialect = _FakeDialect(dialect_name)


class _FakePostgresDb:
    """A minimal stand-in for AsyncSession that only needs to satisfy
    ``_dialect_name``'s ``db.bind.dialect.name`` walk — the actual DB call
    in the postgres branch is monkeypatched at the module-function level in
    these tests, so no real connection is ever made."""

    def __init__(self, dialect_name: str = "postgresql"):
        self.bind = _FakeBind(dialect_name)


@pytest.mark.asyncio
class TestDetectBankPatternPostgresDialectBranch:
    """Unit-level coverage of the postgres/pgvector call path using a fake
    ``db`` (dialect='postgresql') and a monkeypatched
    ``misconception_bank.match_by_embedding`` — no real pgvector connection.
    A genuine pgvector integration test lives outside the unit suite, per
    ``misconception_bank.py``'s own SQLite-bypass docstring."""

    async def test_postgres_path_hit_yields_finding(self, monkeypatch):
        entry = _entry(code="includes_transfers")

        async def _fake_match_by_embedding(db, *, concept_id, query_embedding, k=3):
            return [(entry, 0.93)]

        monkeypatch.setattr(
            "apollo.overseer.misconception_bank.match_by_embedding",
            _fake_match_by_embedding,
        )

        embed_fn = _StubEmbedFn({"GDP includes transfers": _unit_vec(0.0)})
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=42,
            student_utterances=("GDP includes transfers",),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        assert findings[0].confidence == pytest.approx(0.93)
        assert findings[0].signature == "misc.includes_transfers"

    async def test_postgres_path_no_match_yields_no_finding(self, monkeypatch):
        async def _fake_match_by_embedding(db, *, concept_id, query_embedding, k=3):
            return []

        monkeypatch.setattr(
            "apollo.overseer.misconception_bank.match_by_embedding",
            _fake_match_by_embedding,
        )

        entry = _entry()
        embed_fn = _StubEmbedFn({"some utterance": _unit_vec(0.0)})
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=42,
            student_utterances=("some utterance",),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_postgres_path_concept_id_none_yields_no_finding(self):
        entry = _entry()
        embed_fn = _StubEmbedFn({"some utterance": _unit_vec(0.0)})
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=None,
            student_utterances=("some utterance",),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_postgres_path_embed_failure_soft_fails(self):
        class _RaisingEmbedFn:
            def __call__(self, text: str) -> list[float]:
                raise RuntimeError("embedding service down")

        entry = _entry()
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=42,
            student_utterances=("some utterance",),
            embed_fn=_RaisingEmbedFn(),
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_postgres_path_db_error_soft_fails(self, monkeypatch):
        async def _raising_match_by_embedding(db, *, concept_id, query_embedding, k=3):
            raise RuntimeError("connection dropped")

        monkeypatch.setattr(
            "apollo.overseer.misconception_bank.match_by_embedding",
            _raising_match_by_embedding,
        )

        entry = _entry()
        embed_fn = _StubEmbedFn({"some utterance": _unit_vec(0.0)})
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=42,
            student_utterances=("some utterance",),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert findings == ()

    async def test_postgres_path_below_floor_emits_corroboration_only_finding(self, monkeypatch):
        """A10 (corroboration/keying redesign): a below-floor (but positive)
        similarity is now EMITTED, tagged bank_match_above_floor=False,
        rather than abstained — see TestBelowFloorEmission for the dedicated
        SQLite-path coverage of this behavior change."""
        entry = _entry()

        async def _fake_match_by_embedding(db, *, concept_id, query_embedding, k=3):
            return [(entry, 0.10)]

        monkeypatch.setattr(
            "apollo.overseer.misconception_bank.match_by_embedding",
            _fake_match_by_embedding,
        )

        embed_fn = _StubEmbedFn({"some utterance": _unit_vec(0.0)})
        findings = await detect_bank_pattern(
            _FakePostgresDb(),
            concept_id=42,
            student_utterances=("some utterance",),
            embed_fn=embed_fn,
            bank_entries=(entry,),
        )

        assert len(findings) == 1
        assert findings[0].bank_match_above_floor is False
        assert findings[0].confidence == pytest.approx(0.10)


class TestCosineSimilarityHelper:
    def test_zero_vector_returns_zero_not_raise(self):
        from apollo.overseer.misconception_detector.bank_pattern import (
            _cosine_similarity,
        )

        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
        assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0

    def test_empty_or_mismatched_vectors_return_zero(self):
        from apollo.overseer.misconception_detector.bank_pattern import (
            _cosine_similarity,
        )

        assert _cosine_similarity([], [1.0, 0.0]) == 0.0
        assert _cosine_similarity([1.0, 0.0], []) == 0.0
        assert _cosine_similarity([1.0], [1.0, 0.0]) == 0.0


class TestDialectNameHelper:
    def test_none_bind_defaults_to_postgresql(self):
        from apollo.overseer.misconception_detector.bank_pattern import (
            _dialect_name,
        )

        class _NoBindDb:
            bind = None

        assert _dialect_name(_NoBindDb()) == "postgresql"

    def test_bind_access_raising_defaults_to_postgresql(self):
        from apollo.overseer.misconception_detector.bank_pattern import (
            _dialect_name,
        )

        class _BrokenBindDb:
            @property
            def bind(self):
                raise RuntimeError("no engine bound")

        assert _dialect_name(_BrokenBindDb()) == "postgresql"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
