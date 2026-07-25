---
doc: ai-ta-backend/rag-pipeline/vision
description: Image transcription for photo-of-problem attachments on /ask (fail-open OCR fallback).
owns:
  - ai/vision.py
related:
  - ai-ta-backend/rag-pipeline/main-ai
  - ai-ta-backend/platform/config-model-pins
last_verified: 2026-07-25
stub: false
---

# vision — image transcription

## Interface

- `vision_transcribe(image_paths: Sequence[str]) -> str` — OpenAI vision with a
  pytesseract fallback; imported by server.py.
- `vision_direct_answer(image_paths, question_hint="") -> str` — UNUSED by /ask.
- `_file_to_data_url` helper.

## Data flow

In the /ask flow the transcription is distilled by
`main_ai.extract_keywords` and appended to the question as `q_effective` (image
text truncated to a fallback length when needed).

## Invariants & gotchas

- **Fail-open**: any OpenAI/OCR error returns `""` (no attachment text) rather
  than raising; falls back to pytesseract only if installed.

## Env flags

`VISION_MODEL` (transcription), `VISION_ANSWER_MODEL` (direct-answer; falls back
to `config.models.MAIN_MODEL`), `OPENAI_API_KEY` (presence gate).

## Related

`main-ai`, `platform/config-model-pins`.
