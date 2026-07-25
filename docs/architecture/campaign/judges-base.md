---
doc: campaign/judges-base
description: The shared StageJudge framework subclassed by all five S1–S5 stage judges.
owns:
  - campaign/judges/base.py
  - campaign/judges/__init__.py
related:
  - campaign/judges-s1-s2
  - campaign/judges-s3-s4
  - campaign/judges-s5
last_verified: 2026-07-25
stub: false
---

# campaign/judges-base — StageJudge framework

The shared scaffolding: `build_items(raw)` (pure, unit-tested without an LLM),
one schema-constrained LLM call per item, and a deterministic aggregation.

## Interface

- `Verdict(item_id, ok, reason)` and `JudgeResult(stage, verdicts, passed,
  total, pass_rate, extra)` dataclasses (`.failures` property).
- `aggregate(verdicts) -> (passed, total, pass_rate)` — the campaign gate math;
  an empty item set → `(0, 0, 0.0)` (never a false "100% passing").
- `verdict_schema(name) -> dict` — a fresh strict `{ok: bool, reason: str}`
  json_schema per call.
- `load_jsonl(path) -> list[dict]` — missing file → `[]` (zero items, not a raise).
- `JudgeLLM` Protocol (`judge_item`) — the injectable LLM seam.
- `StageJudge` base — subclasses set `stage` + `system_prompt` and implement
  `build_items`/`user_prompt`/`item_id`; `judge` is the only method touching the
  LLM (everything else is pure).
- `OpenAIJudgeClient` — the real `JudgeLLM` (one `gpt-4o` json_schema call per
  item, offloaded to a thread; the network `_call` is pragma-excluded).

## Invariants & gotchas

- Verdicts are schema-validated; the E3 gate reads `aggregate()` pass-rate.
  Per-stage gate bars (95% / 90% / precision-only) are E3's job, not the result's
  — the same `JudgeResult` shape works for every stage.

## Env flags

- `APOLLO_JUDGE_MODEL` (default `gpt-4o`).

## Related

- [judges-s1-s2](judges-s1-s2.md), [judges-s3-s4](judges-s3-s4.md),
  [judges-s5](judges-s5.md) (all `StageJudge` subclasses).
