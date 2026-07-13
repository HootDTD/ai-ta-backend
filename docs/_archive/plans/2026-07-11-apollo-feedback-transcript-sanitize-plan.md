# Apollo Feedback Transcript + Narrative Sanitization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach the graded attempt's chat transcript to the Apollo Done payload (rendered as a collapsed dropdown on the student report), and stop the diagnostic narrative from leaking internal canonical keys, credit/weight decimals, and dock values.

**Architecture:** Backend root-cause fix (the narrative prompt stops receiving internals) plus a deterministic pure-function output gate applied to every narrative, plus one new field on the existing Done response (no new endpoint, no new auth surface). UI renders the new field with the repo's existing `<details>` idiom.

**Tech Stack:** FastAPI + SQLAlchemy async (backend), pytest with the `_old_path_patches` done-route harness, Next.js 15 + TypeScript (student UI).

**Spec:** `docs/_archive/specs/2026-07-11-apollo-feedback-transcript-sanitize-design.md` (same repo).

## Global Constraints

- Backend worktree: `C:\Users\ultra\OneDrive\TA-test\.worktrees\feedback-ux` (branch `feat/apollo-feedback-transcript-sanitize`, off `origin/staging`). ALL backend paths below are relative to it.
- UI worktree: `C:\Users\ultra\OneDrive\TA-test\.worktrees\feedback-ux-ui` (same branch name). Task 6 paths are relative to it.
- Python for all backend commands: `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python` (the main checkout's venv; verified to import `apollo` from the worktree cwd). Run all backend commands from the backend worktree root.
- Patch coverage of changed backend lines must be ≥95% (`diff-cover --compare-branch=origin/staging --fail-under=95`).
- The transcript field and the sanitizer are NOT flag-gated — they apply unconditionally.
- Narrative rules (user-approved): whole-number percentages that the topic list already shows are ALLOWED; canonical keys, `credit=`/`weight=` decimals, dock values, and scoring mechanics are NEVER allowed.
- Do not touch `apollo/grading/diagnostic.py` (dormant graph-sim narrative path).
- Conventional commit messages; never push to `main`/`ApolloV3`; PRs target `staging`.
- No new packages.
- UI repo has no test runner: list untested UI changes explicitly in the PR description.

---

### Task 1: `sanitize_narrative` output gate (backend)

**Files:**
- Modify: `apollo/overseer/topic_narrative.py`
- Test: `apollo/overseer/tests/test_topic_narrative_sanitize.py` (create)

**Interfaces:**
- Produces: `sanitize_narrative(text: str, canonical_keys: Sequence[str] = ()) -> str` — pure, idempotent; exported in `__all__`. Task 3 imports it from `apollo.overseer.topic_narrative`.

- [ ] **Step 1: Write the failing tests**

Create `apollo/overseer/tests/test_topic_narrative_sanitize.py`:

```python
"""2026-07-11 feedback spec §2 — deterministic internals gate on the narrative.

The gate is the belt-and-suspenders layer under the prompt fix: even if the
LLM leaks ledger internals (canonical keys, credit/weight decimals, dock
values), the served narrative must not contain them. Percentages the topic
list already shows are allowed and must survive.
"""

from __future__ import annotations

import pytest

from apollo.overseer.topic_narrative import sanitize_narrative

pytestmark = pytest.mark.unit

# The exact leak observed live on staging (attempt 62, MGMT course).
_LEAKY = (
    "You clearly explained the directional relationship between upstream and "
    "downstream as movement from source to destination "
    "(proc_explain_directionality, credit 0.80, weight 0.77). You also "
    "successfully described why this matters "
    "(proc_explain_causality, credit 0.90, weight 0.23).\n\n"
    "No points were docked for errors (misconception dock: 0.000)."
)
_KEYS = ["proc_explain_directionality", "proc_explain_causality"]


def test_strips_observed_staging_leak():
    out = sanitize_narrative(_LEAKY, canonical_keys=_KEYS)
    assert "proc_explain_directionality" not in out
    assert "proc_explain_causality" not in out
    assert "credit" not in out.lower()
    assert "weight 0.77" not in out
    # The internal fragment goes; the plain-English word "docked" is fine.
    assert "dock: 0.000" not in out
    assert "0.80" not in out and "0.23" not in out and "0.000" not in out
    # Prose survives, including the sentence that lost its parenthetical.
    assert "directional relationship" in out
    assert "No points were docked for errors" in out


def test_no_empty_parens_or_dangling_punctuation_left_behind():
    out = sanitize_narrative(_LEAKY, canonical_keys=_KEYS)
    assert "()" not in out
    assert "( ," not in out and "(, " not in out
    assert " ." not in out and " ," not in out


def test_percentages_are_preserved():
    text = "You earned 80% on the causality topic and 100% on the definition."
    assert sanitize_narrative(text, canonical_keys=_KEYS) == text


def test_inline_scoring_fragments_without_parens_are_stripped():
    text = "That topic had credit=0.80 and weight: 0.77 overall."
    out = sanitize_narrative(text, canonical_keys=[])
    assert "0.80" not in out and "0.77" not in out
    assert "credit" not in out.lower() and "weight" not in out.lower()


def test_physics_weight_prose_is_not_stripped():
    # "weight" as a physics word (no 0-1 decimal after it) must survive.
    text = "The weight of the fluid column ($w = mg$) pushes down, so weight = mg."
    assert sanitize_narrative(text, canonical_keys=[]) == text


def test_math_spans_preserved():
    text = "Bernoulli: $P + 0.5 \\rho v^2 = const$ along a streamline."
    assert sanitize_narrative(text, canonical_keys=[]) == text


def test_idempotent():
    once = sanitize_narrative(_LEAKY, canonical_keys=_KEYS)
    assert sanitize_narrative(once, canonical_keys=_KEYS) == once


def test_backticked_keys_stripped():
    text = "You missed `def.def_future_shock` here."
    out = sanitize_narrative(text, canonical_keys=["def.def_future_shock"])
    assert "def_future_shock" not in out
    assert "`" not in out


def test_general_bucket_key_and_empty_keys_are_safe():
    text = "Other issues were minor."
    assert sanitize_narrative(text, canonical_keys=["_general", ""]) == text


def test_empty_and_placeholder_text_untouched():
    assert sanitize_narrative("", canonical_keys=[]) == ""
    placeholder = "[Diagnostic narrative unavailable — the grade above is still accurate.]"
    assert sanitize_narrative(placeholder, canonical_keys=[]) == placeholder
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from the backend worktree root):
```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/overseer/tests/test_topic_narrative_sanitize.py -q
```
Expected: FAIL — `ImportError: cannot import name 'sanitize_narrative'`.

- [ ] **Step 3: Implement `sanitize_narrative`**

In `apollo/overseer/topic_narrative.py`: add `import re` and `from collections.abc import Sequence` to the stdlib import block ABOVE the `from apollo.overseer.topic_score import TopicScoreResult` line (isort order: stdlib before first-party), then add below the imports:

```python

# Scoring internals are 0-1 decimals (credit 0.80, weight 0.77, dock 0.000).
# Requiring that shape keeps legitimate prose like "weight = mg" or
# "$0.5 \rho v^2$" intact while still catching every ledger-shaped leak.
_SCORING_NUM = r"-?[01]?\.\d+"
_SCORING_TERM = (
    rf"\b(?:credit|weight|dock(?:ed)?|misconception[ _]dock)\b\s*[:=]?\s*{_SCORING_NUM}"
)
_SCORING_PAREN_RE = re.compile(rf"\(\s*[^()]*?{_SCORING_TERM}[^()]*?\)", re.IGNORECASE)
_SCORING_INLINE_RE = re.compile(_SCORING_TERM, re.IGNORECASE)
_EMPTY_PAREN_RE = re.compile(r"\(\s*[,;\s]*\)")
_DANGLING_COMMA_RE = re.compile(r",\s*(?=[,.;:)])")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?])")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def sanitize_narrative(text: str, canonical_keys: Sequence[str] = ()) -> str:
    """Deterministic gate: strip ledger internals from a narrative.

    Belt-and-suspenders under the prompt fix (2026-07-11 feedback spec §2) —
    the prompt no longer contains canonical keys/weights, but the narrative is
    LLM output, so the served text is scrubbed regardless. Pure + idempotent;
    returns a new string. Whole-number percentages (the topic list's own
    numbers) are deliberately preserved.
    """
    cleaned = text
    for key in canonical_keys:
        if not key or key == "_general":
            continue
        cleaned = re.sub(rf"`?\b{re.escape(key)}\b`?", "", cleaned)
    cleaned = _SCORING_PAREN_RE.sub("", cleaned)
    cleaned = _SCORING_INLINE_RE.sub("", cleaned)
    cleaned = _EMPTY_PAREN_RE.sub("", cleaned)
    cleaned = _DANGLING_COMMA_RE.sub("", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned
```

Update the module's `__all__`:

```python
__all__ = ["build_topic_narrative_prompt", "sanitize_narrative"]
```

- [ ] **Step 4: Run tests to verify they pass**

Same command as Step 2. Expected: all PASS. If a whitespace/punctuation expectation fails, fix the regex (not the test's intent: no keys, no scoring decimals, no artifacts).

- [ ] **Step 5: Commit**

```bash
git add apollo/overseer/topic_narrative.py apollo/overseer/tests/test_topic_narrative_sanitize.py
git commit -m "feat: sanitize_narrative gate strips ledger internals from narratives"
```

---

### Task 2: Narrative prompt stops receiving internals (backend)

**Files:**
- Modify: `apollo/overseer/topic_narrative.py` (`_TOPIC_SYSTEM_PROMPT`, `_format_topic_line`, `build_topic_narrative_prompt`)
- Test: `apollo/overseer/tests/test_topic_narrative_prompt.py` (create)
- Modify: `apollo/overseer/tests/test_diagnostic_topic_score.py:154` (one stale assertion)

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_topic_narrative_prompt` — same signature `(result: TopicScoreResult, *, problem_text: str) -> tuple[str, str]`, but the returned `user` string now contains display names + statuses + whole-number percentages only.

- [ ] **Step 1: Write the failing tests**

Create `apollo/overseer/tests/test_topic_narrative_prompt.py`:

```python
"""2026-07-11 feedback spec §2 — the prompt itself must not contain internals.

Root-cause fix: the LLM can't leak canonical keys / credit / weight / dock if
they never reach the prompt. Display names + statuses + whole-number
percentages are the only per-topic data the narrator sees.
"""

from __future__ import annotations

import pytest

from apollo.overseer.topic_narrative import build_topic_narrative_prompt
from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult

pytestmark = pytest.mark.unit


def _result(display_name: str | None = "Explain causality in directional systems") -> TopicScoreResult:
    return TopicScoreResult(
        score=64,
        letter="C",
        coverage_component=0.6359,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="proc_explain_causality",
                display_name=display_name,
                credit=0.9,
                status="covered",
                weight=0.23,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.wrong_direction",
                        resolved=True,
                        dock_points=0.05,
                        evidence_span="downstream changes rewrite the source",
                    ),
                ),
            ),
        ),
    )


def test_user_prompt_has_no_canonical_keys_or_decimals():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="Explain upstream vs downstream.")
    assert "proc_explain_causality" not in user
    assert "misc.wrong_direction" not in user
    assert "credit=" not in user and "weight=" not in user
    assert "0.23" not in user and "0.6359" not in user
    assert "Coverage component" not in user
    assert "Misconception dock" not in user


def test_user_prompt_carries_display_name_status_and_percent():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "Explain causality in directional systems" in user
    assert "covered" in user
    assert "90%" in user
    assert "Score: 64 (C)" in user


def test_misconception_line_keeps_span_and_resolution_only():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "downstream changes rewrite the source" in user
    assert "corrected" in user
    assert "0.05" not in user


def test_missing_display_name_falls_back_to_humanized_key():
    _system, user = build_topic_narrative_prompt(_result(display_name=None), problem_text="P?")
    assert "proc_explain_causality" not in user
    assert "explain causality" in user


def test_system_prompt_forbids_internals_and_allows_percentages():
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "internal identifiers" in system
    assert "percentage" in system.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/overseer/tests/test_topic_narrative_prompt.py -q
```
Expected: FAIL (keys/decimals present, no humanized fallback, no internals rule).

- [ ] **Step 3: Rewrite the prompt builders**

In `apollo/overseer/topic_narrative.py`:

(a) Replace `_format_topic_line` and add `_humanize_key` above it:

```python
def _humanize_key(key: str) -> str:
    """Presentation fallback when a topic has no display_name.

    The narrator quotes whatever it sees, so the raw snake_case key must
    never reach the prompt — degrade to a readable phrase instead.
    """
    tail = key.rsplit(".", 1)[-1]
    for prefix in ("def_", "proc_", "eq_", "cond_"):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
            break
    return tail.replace("_", " ").strip() or "this topic"


def _format_topic_line(topic) -> str:  # noqa: ANN001 - TopicCredit, avoid import cycle noise
    name = topic.display_name or _humanize_key(topic.canonical_key)
    pct = round(topic.credit * 100)
    line = f'- Topic "{name}": {_status_label(topic.status)} — {pct}%'
    if topic.misconceptions:
        for m in topic.misconceptions:
            resolved = "corrected" if m.resolved else "uncorrected"
            span = m.evidence_span if m.evidence_span else "(no evidence span)"
            line += f'\n  * Misconception ({resolved}): "{span}"'
    return line
```

(b) In `build_topic_narrative_prompt`, replace the `user = (...)` assembly with:

```python
    user = (
        f"Problem: {problem_text}\n\n"
        f"Score: {result.score} ({result.letter})\n\n"
        f"Ledger:\n{topic_lines}\n"
    )
```

(c) In `_TOPIC_SYSTEM_PROMPT`, replace the misconception bullet

```
- For every misconception in the ledger, quote its evidence span verbatim (or close
  paraphrase of the quoted text) and state its point cost. If it is marked resolved, praise the
  correction explicitly — do not describe it as still wrong.
```

with

```
- For every misconception in the ledger, quote its evidence span verbatim (or close
  paraphrase of the quoted text). If it is marked corrected, praise the correction
  explicitly — do not describe it as still wrong.
- NEVER surface internal identifiers (snake_case keys), decimal credit/weight/dock
  values, or any hint of how the score is computed internally. You may cite the same
  whole-number percentages the ledger shows (e.g. "80%").
```

(d) Update the docstring of `build_topic_narrative_prompt`: replace the sentence mentioning "status/credit/weight" with "status and whole-number percentage (display names only — internals never reach the prompt; see `sanitize_narrative` for the output-side gate)".

- [ ] **Step 4: Fix the one stale existing assertion**

In `apollo/overseer/tests/test_diagnostic_topic_score.py`, test `test_flag_on_with_topic_score_uses_ledger_grounded_prompt` currently asserts the canonical key is IN the prompt:

```python
    assert "p1" in user_msg["content"]
    assert "apply continuity" in user_msg["content"]
```

Replace with:

```python
    # 2026-07-11 feedback spec §2: canonical keys never reach the prompt —
    # the display name is the topic's only identity in the ledger text.
    assert "apply continuity" in user_msg["content"]
    assert "credit=" not in user_msg["content"]
    assert "weight=" not in user_msg["content"]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/overseer/tests/test_topic_narrative_prompt.py apollo/overseer/tests/test_topic_narrative_sanitize.py apollo/overseer/tests/test_diagnostic_topic_score.py -q
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apollo/overseer/topic_narrative.py apollo/overseer/tests/test_topic_narrative_prompt.py apollo/overseer/tests/test_diagnostic_topic_score.py
git commit -m "feat: ledger narrative prompt drops canonical keys, weights, and dock values"
```

---

### Task 3: Wire the gate into `generate_diagnostic` (backend)

**Files:**
- Modify: `apollo/overseer/diagnostic.py`
- Test: `apollo/overseer/tests/test_diagnostic_sanitize.py` (create)
- Modify: `docs/architecture/apollo.md` (narrative section; same commit)

**Interfaces:**
- Consumes: `sanitize_narrative` from Task 1.
- Produces: `generate_diagnostic` — same signature; every return value now passes through `sanitize_narrative`.

- [ ] **Step 1: Write the failing tests**

Create `apollo/overseer/tests/test_diagnostic_sanitize.py`:

```python
"""2026-07-11 feedback spec §2 — generate_diagnostic applies the internals
gate to EVERY narrative (topic path, legacy axis path, and the deterministic
append lines), as the last step before return."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit

_COVERAGE = {"per_step": {}, "procedure_scores": {}}
_RUBRIC = {
    "overall": {"score": 64, "letter": "C"},
    "procedure": {"score": 64, "letter": "C", "present": True},
    "justification": {"score": 0, "letter": "F", "present": False},
    "simplification": {"score": 0, "letter": "F", "present": False},
}

_LEAKY_LLM_OUTPUT = (
    "Great work on causality (proc_explain_causality, credit 0.90, weight 0.23). "
    "Misconception dock: 0.000.\n\nNext step: keep going."
)


def _topic_score() -> TopicScoreResult:
    return TopicScoreResult(
        score=64,
        letter="C",
        coverage_component=0.64,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="proc_explain_causality",
                display_name="Explain causality",
                credit=0.9,
                status="covered",
                weight=0.23,
                misconceptions=(),
            ),
        ),
    )


def _mock_client_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=text))]
    )
    return client


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("APOLLO_TOPIC_SCORE_SERVED", raising=False)
    yield


@patch("apollo.overseer.diagnostic.OpenAI")
def test_topic_path_output_is_sanitized(mock_client_cls, monkeypatch):
    monkeypatch.setenv("APOLLO_TOPIC_SCORE_SERVED", "true")
    mock_client_cls.return_value = _mock_client_returning(_LEAKY_LLM_OUTPUT)

    out = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )
    assert "proc_explain_causality" not in out
    assert "credit" not in out.lower()
    assert "0.23" not in out and "0.90" not in out and "0.000" not in out
    assert "Great work on causality" in out
    assert "Next step:" in out


@patch("apollo.overseer.diagnostic.OpenAI")
def test_legacy_axis_path_output_is_sanitized_too(mock_client_cls, monkeypatch):
    monkeypatch.delenv("APOLLO_TOPIC_SCORE_SERVED", raising=False)
    mock_client_cls.return_value = _mock_client_returning(_LEAKY_LLM_OUTPUT)

    out = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
    )
    # No topic_score -> no key list, but pattern-based scrubbing still applies.
    assert "credit" not in out.lower()
    assert "0.90" not in out


@patch("apollo.overseer.diagnostic.OpenAI")
def test_clean_narrative_passes_through_unchanged(mock_client_cls, monkeypatch):
    monkeypatch.setenv("APOLLO_TOPIC_SCORE_SERVED", "true")
    clean = "You covered causality well (80%).\n\nNext step: cover the overload idea."
    mock_client_cls.return_value = _mock_client_returning(clean)

    out = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )
    assert out == clean


@patch("apollo.overseer.diagnostic.OpenAI")
def test_soft_fail_placeholder_survives_sanitizer(mock_client_cls, monkeypatch):
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("boom")
    mock_client_cls.return_value = client

    out = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
    )
    assert out == "[Diagnostic narrative unavailable — the grade above is still accurate.]"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/overseer/tests/test_diagnostic_sanitize.py -q
```
Expected: the two sanitize tests FAIL (leaky text passes through today); the clean/soft-fail tests may already pass.

- [ ] **Step 3: Wire the sanitizer**

In `apollo/overseer/diagnostic.py`:

(a) Extend the import (line 28):

```python
from apollo.overseer.topic_narrative import build_topic_narrative_prompt, sanitize_narrative
```

(b) Replace the tail of `generate_diagnostic` (currently lines 120–122):

```python
    narrative = _append_misconception_line(narrative, rubric)
    narrative = _append_negotiation_line(narrative, coverage)
    return narrative
```

with:

```python
    narrative = _append_misconception_line(narrative, rubric)
    narrative = _append_negotiation_line(narrative, coverage)
    # 2026-07-11 feedback spec §2: deterministic internals gate on EVERY
    # narrative (topic + legacy paths + append lines) — the student never
    # sees canonical keys, credit/weight decimals, or dock values, even if
    # the LLM ignores the prompt rules. No-op on clean prose.
    ledger_keys = (
        tuple(t.canonical_key for t in topic_score.topics) if topic_score is not None else ()
    )
    return sanitize_narrative(narrative, canonical_keys=ledger_keys)
```

(c) In the module docstring (lines 9–16) and the `generate_diagnostic` docstring (lines 71–79), amend the two "byte-identical" claims: append "modulo the final internals sanitizer (`sanitize_narrative`, 2026-07-11 feedback spec §2), which is a no-op on clean prose" to each claim.

- [ ] **Step 4: Run the overseer suite**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/overseer -q
```
Expected: all PASS (the existing `test_diagnostic_topic_score.py` equality tests compare two sanitized outputs, so they stay green).

- [ ] **Step 5: Reconcile the owner doc**

In `docs/architecture/apollo.md`: Grep for `topic_narrative` / `diagnostic narrative` within the file; in the narrative/diagnostic paragraph add one sentence: "As of 2026-07-11, the ledger prompt carries display names + whole-number percentages only (no canonical keys/weights/docks), and every narrative passes through `sanitize_narrative` (`overseer/topic_narrative.py`) — a deterministic internals gate — before serving." Bump the frontmatter `last_verified` to `2026-07-11`.

- [ ] **Step 6: Commit**

```bash
git add apollo/overseer/diagnostic.py apollo/overseer/tests/test_diagnostic_sanitize.py docs/architecture/apollo.md
git commit -m "feat: apply sanitize_narrative gate to every served diagnostic narrative"
```

---

### Task 4: Attempt transcript on the Done payload (backend)

**Files:**
- Modify: `apollo/handlers/done.py` (helper + attach)
- Modify: `apollo/handlers/tests/test_done_shadow_flag.py` (`_old_path_patches`)
- Modify: `apollo/handlers/tests/test_done_graph_grader_live.py` (~line 130 golden)
- Modify: `apollo/handlers/tests/test_done_shadow_isolation.py` (~line 72 golden)
- Test: `apollo/handlers/tests/test_done_transcript.py` (create)
- Modify: `docs/architecture/apollo.md` (Done payload; same commit)

**Interfaces:**
- Consumes: `Message` model (already imported in `done.py` line 83), `select` (line 23), `_LOG` (line 93).
- Produces: `_fetch_attempt_transcript(db, attempt_id) -> list[dict[str, Any]]` in `apollo.handlers.done` (patchable collaborator), and `student_response["transcript"]` on the Done payload. Task 6's UI type `TranscriptTurn {role, content, turn_index}` mirrors this shape; roles are `"student"` / `"apollo"`.

- [ ] **Step 1: Write the failing tests**

Create `apollo/handlers/tests/test_done_transcript.py`:

```python
"""2026-07-11 feedback spec §1 — attempt-scoped transcript on the Done payload.

The transcript is presentation data: it rides the existing owner-gated Done
response (no new endpoint) and soft-fails to [] without voiding the grade.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apollo.handlers.done import _fetch_attempt_transcript, handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches

pytestmark = pytest.mark.unit


def _msg(role: str, content: str, turn_index: int) -> MagicMock:
    m = MagicMock()
    m.role = role
    m.content = content
    m.turn_index = turn_index
    return m


class _MsgResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        m = MagicMock()
        m.all.return_value = self._rows
        return m


def _patches_without_transcript_stub(patches):
    return [
        p for p in patches if getattr(p, "attribute", None) != "_fetch_attempt_transcript"
    ]


async def _run_done(db, patches):
    for p in patches:
        p.start()
    try:
        return await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()


async def test_done_payload_includes_attempt_transcript():
    db, _sess, _attempt, patches = _old_path_patches()
    patches = _patches_without_transcript_stub(patches)

    rows = [_msg("student", "hi Apollo", 0), _msg("apollo", "hi! teach me", 1)]
    orig_execute = db.execute.side_effect
    calls = {"n": 0}

    async def _execute(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:  # 1 = session, 2 = attempt (harness contract)
            return await orig_execute(*a, **kw)
        return _MsgResult(rows)

    db.execute = AsyncMock(side_effect=_execute)

    out = await _run_done(db, patches)
    assert out["transcript"] == [
        {"role": "student", "content": "hi Apollo", "turn_index": 0},
        {"role": "apollo", "content": "hi! teach me", "turn_index": 1},
    ]


async def test_transcript_db_failure_soft_fails_to_empty_list():
    db, _sess, _attempt, patches = _old_path_patches()
    patches = _patches_without_transcript_stub(patches)

    orig_execute = db.execute.side_effect
    calls = {"n": 0}

    async def _execute(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            return await orig_execute(*a, **kw)
        raise RuntimeError("db connection dropped")

    db.execute = AsyncMock(side_effect=_execute)

    out = await _run_done(db, patches)
    # Grade intact, transcript degraded.
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["transcript"] == []


async def test_fetch_helper_returns_ordered_dicts():
    rows = [_msg("student", "a", 0), _msg("apollo", "b", 1)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=_MsgResult(rows))

    out = await _fetch_attempt_transcript(db, 42)
    assert out == [
        {"role": "student", "content": "a", "turn_index": 0},
        {"role": "apollo", "content": "b", "turn_index": 1},
    ]


async def test_fetch_helper_soft_fails():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    assert await _fetch_attempt_transcript(db, 42) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/handlers/tests/test_done_transcript.py -q
```
Expected: FAIL — `ImportError: cannot import name '_fetch_attempt_transcript'`.

- [ ] **Step 3: Implement helper + attach**

In `apollo/handlers/done.py`:

(a) Insert directly above `async def handle_done(` (~line 542):

```python
async def _fetch_attempt_transcript(
    db: AsyncSession, attempt_id: int
) -> list[dict[str, Any]]:
    """Attempt-scoped chat transcript for the student-facing Done payload.

    2026-07-11 feedback spec §1: the report view's dropdown shows exactly the
    conversation the grade was based on. Presentation data only — soft-fails
    to [] (logged) so a transcript hiccup never voids the committed grade.
    """
    try:
        msgs = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.attempt_id == attempt_id)
                    .order_by(Message.turn_index)
                )
            )
            .scalars()
            .all()
        )
        return [
            {"role": m.role, "content": m.content, "turn_index": m.turn_index}
            for m in msgs
        ]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "transcript fetch soft-fail for attempt %s: %s", attempt_id, exc, exc_info=True
        )
        return []
```

(b) After the `if serve_topic_score:` block (~lines 944–945: `student_response["topics"] = serialize_topics(topic_score)`), add:

```python
    # Attempt-scoped transcript (2026-07-11 feedback spec §1). Unconditional
    # (not flag-gated); [] on soft-fail.
    student_response["transcript"] = await _fetch_attempt_transcript(db, attempt.id)
```

(c) In `apollo/handlers/tests/test_done_shadow_flag.py`, append to the `patches` list inside `_old_path_patches` (after the `compute_progress_envelope` patch, line ~142):

```python
        patch(
            "apollo.handlers.done._fetch_attempt_transcript",
            new=AsyncMock(return_value=[]),
        ),
```

(d) Update the two full-payload goldens — add `"transcript": [],` to each golden dict:
- `apollo/handlers/tests/test_done_graph_grader_live.py` (the `assert out == {...}` at ~line 130)
- `apollo/handlers/tests/test_done_shadow_isolation.py` (the golden dict at ~line 72)

- [ ] **Step 4: Run the handler suite**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo/handlers -q
```
Expected: all PASS. If another test asserts the exact full Done payload, add `"transcript": []` to its golden the same way (the harness stub makes `[]` deterministic).

- [ ] **Step 5: Reconcile the owner doc**

In `docs/architecture/apollo.md`: Grep for the Done payload description (search `diagnostic_narrative` or `student_response`); add the `transcript` field to the documented shape: `transcript: [{role: "student"|"apollo", content, turn_index}] — the graded attempt's messages, [] on soft-fail (2026-07-11 feedback spec §1)`. `last_verified` already bumped in Task 3; keep it `2026-07-11`.

- [ ] **Step 6: Commit**

```bash
git add apollo/handlers/done.py apollo/handlers/tests/test_done_transcript.py apollo/handlers/tests/test_done_shadow_flag.py apollo/handlers/tests/test_done_graph_grader_live.py apollo/handlers/tests/test_done_shadow_isolation.py docs/architecture/apollo.md
git commit -m "feat: attempt-scoped transcript on the Apollo Done payload"
```

---

### Task 5: Full backend suite + 95% patch-coverage gate

**Files:** none (verification only)

- [ ] **Step 1: Full apollo suite**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo -q
```
Expected: PASS (same pass/skip counts as the pre-change baseline plus the new tests; baseline had 1 documented legacy skip in `test_done.py`).

- [ ] **Step 2: Patch coverage**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m pytest apollo -q --cov=apollo --cov-report=xml
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```
Expected: `diff-cover` reports ≥95% on the changed lines. If any changed line is uncovered, add a test for it (the only allowed exemption is a line untestable without a prod refactor, documented in the PR description).

- [ ] **Step 3: Lint/format check**

```bash
C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python -m ruff check apollo/overseer/topic_narrative.py apollo/overseer/diagnostic.py apollo/handlers/done.py apollo/overseer/tests/test_topic_narrative_sanitize.py apollo/overseer/tests/test_topic_narrative_prompt.py apollo/overseer/tests/test_diagnostic_sanitize.py apollo/handlers/tests/test_done_transcript.py
```
Expected: no errors. Fix and amend into the relevant commit if trivial, or add a `chore:` commit.

---

### Task 6: Student UI — transcript dropdown + type sync

All paths in this task are relative to the UI worktree `C:\Users\ultra\OneDrive\TA-test\.worktrees\feedback-ux-ui`.

**Files:**
- Modify: `lib/apollo/api.ts` (types, ~line 195–229)
- Modify: `components/apollo/ApolloReportPanel.tsx`
- Modify: `app/globals.css` (near the `.apollo-topic` rules)
- Modify: `docs/architecture/components.md` + `docs/architecture/_overview.md` (same commit)

**Interfaces:**
- Consumes: `transcript?: [{role: "student"|"apollo", content, turn_index}]` from Task 4's Done payload.
- Produces: n/a (leaf).

- [ ] **Step 1: Add the types**

In `lib/apollo/api.ts`, insert after the `TopicCredit` interface (ends ~line 202):

```typescript
// Attempt-scoped chat transcript (2026-07-11 feedback spec §1): the turns
// between starting this problem and clicking Done — the conversation the
// grade was based on. [] when the backend's fetch soft-failed; absent on
// older backends.
export interface TranscriptTurn {
  role: "student" | "apollo";
  content: string;
  turn_index: number;
}
```

and add to `DoneResponse` (after the `topics?: TopicCredit[];` member):

```typescript
  // Attempt-scoped transcript for the report view's dropdown (2026-07-11
  // feedback spec §1). Absent on older backends; [] on backend soft-fail.
  transcript?: TranscriptTurn[];
```

- [ ] **Step 2: Render the dropdown**

In `components/apollo/ApolloReportPanel.tsx`:

(a) Extend the type import (line 4):

```typescript
import type { DoneResponse, Rubric, RubricAxis, TopicCredit, TranscriptTurn } from "@/lib/apollo/api";
```

(b) Add above `export default function ApolloReportPanel`:

```tsx
function TranscriptSection({ transcript }: { transcript: TranscriptTurn[] }) {
  return (
    <details>
      <summary>Your conversation with Apollo</summary>
      <div className="apollo-transcript">
        {transcript.map((t) => (
          <div
            key={t.turn_index}
            className="apollo-transcript__turn"
            data-role={t.role}
          >
            <span className="apollo-transcript__role">
              {t.role === "student" ? "You" : "Apollo"}
            </span>
            <div className="prose md-body">
              <MathMarkdown>{t.content}</MathMarkdown>
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}
```

(c) In `ApolloReportPanel`, next to the `hasTopics` derivation (~line 146), add:

```tsx
  // Network data: absent on older backends, [] on backend soft-fail — the
  // section renders only when there are turns to show.
  const transcript = report.transcript;
  const hasTranscript = Array.isArray(transcript) && transcript.length > 0;
```

(d) Between the narrative `</details>` (line 197) and `<div className="composer-foot">` (line 199), add:

```tsx
      {hasTranscript && (
        <TranscriptSection transcript={transcript as TranscriptTurn[]} />
      )}
```

Note the new `<details>` is deliberately collapsed by default (no `open` attribute) — the narrative one keeps its `open`.

- [ ] **Step 3: Add the CSS**

In `app/globals.css`, directly after the last `.apollo-topic__*` rule, add:

```css
.apollo-transcript {
  margin-top: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  max-height: 20rem;
  overflow-y: auto;
}

.apollo-transcript__turn {
  border-left: 2px solid var(--border, #d0d0d0);
  padding-left: 0.75rem;
}

.apollo-transcript__turn[data-role="student"] {
  border-left-color: var(--accent, #6b8afd);
}

.apollo-transcript__role {
  display: block;
  font-size: 0.75rem;
  font-weight: 600;
  opacity: 0.7;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
```

If `globals.css` already defines theme variables with different names (check the top of the file), use those variable names instead of `--border`/`--accent`, keeping the literal fallbacks.

- [ ] **Step 4: Verify (no test runner in this repo)**

```bash
npx tsc --noEmit
npm run lint
```
Expected: both clean. Record in the eventual PR description: "UI changes are untested (no runner wired); verified via tsc + eslint + manual run" per the workspace test-coverage contract.

- [ ] **Step 5: Reconcile the owner docs**

- `docs/architecture/components.md`: in the `ApolloReportPanel` entry, add: "Collapsed 'Your conversation with Apollo' `<details>` section renders `report.transcript` (attempt-scoped turns, role-labelled You/Apollo through `MathMarkdown`); hidden when absent/empty." Bump `last_verified: 2026-07-11`.
- `docs/architecture/_overview.md`: in the `lib/apollo/api.ts` description, add `TranscriptTurn` + `DoneResponse.transcript?`. Bump `last_verified: 2026-07-11`.

- [ ] **Step 6: Commit**

```bash
git add lib/apollo/api.ts components/apollo/ApolloReportPanel.tsx app/globals.css docs/architecture/components.md docs/architecture/_overview.md
git commit -m "feat: transcript dropdown on the Apollo report panel"
```

---

### Task 7: End-to-end smoke (local, optional but recommended)

**Files:** none (verification only)

- [ ] **Step 1:** Start the backend from the backend worktree (`python server.py`, port 8000, needs `.env` — copy from the main checkout `ai-ta-backend/.env` if absent) and the UI from the UI worktree (`npm run dev`, port 3001).
- [ ] **Step 2:** Run one Apollo session to Done; confirm (a) the grade view shows the collapsed "Your conversation with Apollo" dropdown with exactly this attempt's turns, (b) the narrative contains no snake_case keys / `credit` / `weight` / `dock` fragments.
- [ ] **Step 3:** If either fails, debug before opening PRs; capture what was verified for the PR descriptions.
