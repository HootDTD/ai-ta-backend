# Spec: Apollo leak-guard relaxation — allow common English, keep rubric protection

Date: 2026-07-15 · Author: styx conductor (claude) · Implementer: codex
Repo: `ai-ta-backend` · Work IN the existing worktree
`C:\Users\ultra\OneDrive\TA-test\.worktrees\apollo-question-dedup-retry`,
branch `fix/apollo-question-dedup-retry` (head `6657952`, = open PR #182).
New commits on top; no push, no PR (conductor updates PR #182).

## Evidence (staging debug log, deploy 6f1c4e4d, 23:10Z)

One turn, three defects:

1. Draft question "What's one period when you think this 'too quickly to keep
   up' feeling started happening, and what's one example of the kind of
   change from that time?" — better pedagogy than the canned fallback — was
   rejected as `question_vocabulary_boundary` for tokens
   `period, feeling, started, kind, change, time`. All ordinary English;
   "started" is a morphological variant of the public problem's "start".
2. Ack "So far you've basically defined Future Shock as when things are
   happening too quickly…" — a faithful paraphrase of the student's own
   sentence — was rejected (`unsafe_acknowledgement`) for "basically" /
   "defined". This is why Apollo speaks bare questions with no connective
   tissue.
3. `fallback_reason=…_retry_failed` was logged although the redraft's
   QUESTION was accepted and served — only its ack was dropped. Mislabel.

Threat model the guard must keep enforcing: private-rubric content — dates
(1970), proper nouns (Toffler), technical terms, verbatim/near-verbatim
rubric phrases. Ordinary conversational vocabulary is NOT a leak vector.

## Changes (`apollo/smart_questions/unified.py` + new data module)

### 1. Vendored common-English allowlist

- New module `apollo/smart_questions/common_words.py` exposing
  `COMMON_ENGLISH_WORDS: frozenset[str]` — roughly 3000 lowercase common
  English words (conversational/general vocabulary), alphabetized, one per
  line, with a provenance comment (static, generated from general English
  frequency knowledge; no runtime dependency, no network).
- Construction rules: lowercase only; NO numerals or tokens containing
  digits; no single/double-letter entries; MUST include the observed false
  positives (`period, feeling, started, kind, change, time, basically,
  defined`) and their obvious inflections.
- Add this set to the safe tokens in `_private_content_violations` for BOTH
  question and acknowledgement validation.
- Unchanged protections: `token.isdigit()` tokens stay blocked unless in
  public/student text; proper nouns and technical terms won't be on the list
  (lowercase common words only) so they stay blocked; the private-strings
  phrase-containment check stays byte-identical — multi-word rubric phrases
  still die even when composed of common words. Accepted residual risk
  (document in apollo.md): single common words that also appear in the
  rubric are now allowed (e.g. "change"); a single common token cannot
  reproduce rubric content and phrase containment covers compositions.

### 2. Light morphological normalization (reduces rejections only)

- When testing whether a draft token is safe, also compare a suffix-stripped
  form: strip one of `ing`, `ed`, `es`, `s` (longest first, min remaining
  stem 3 chars) from BOTH the draft token and each safe token, and accept on
  normalized match. "started"→"start" (public problem), "occurs"→"occur".
- Apply in the safe-token membership test only — it must never cause a
  rejection that today's code would not make.

### 3. Acknowledgement check keeps its special rule but gains the allowance

- Ack validation gets the common-words set and the morphological match too.
- Everything else about acks unchanged: safe set still excludes the public
  problem text (asserting the problem is still disallowed), "?" in ack still
  rejected, echo check unchanged.

### 4. Honest retry labels

- `_retry_failed` ONLY when the final served question came from the canned
  fallback. If the redraft's question passed and only its acknowledgement
  was dropped, label `…_retry_recovered` (the served question IS the
  model's). Keep `unsafe_acknowledgement` visible in the debug line's
  redraft_validation field as today.

## Tests (diff-cover ≥95 vs origin/staging; suite green)

- Regression from the live log: the exact draft question above, against the
  Future Shock problem text + the two student messages — now PASSES the
  vocabulary check. The exact ack above — now survives ack validation.
- Still-blocked cases: a question introducing "1970" (digits) or a
  capitalized proper noun not in public/student text ("Toffler"); a question
  containing a ≥4-char normalized private-rubric phrase built of common
  words; an ack asserting public-problem wording.
- Morphology: "started"/"occurs" accepted when "start"/"occur" are safe; a
  3-char stem edge case does not over-match.
- Retry label: draft rejected → redraft question passes, ack dropped →
  fallback_reason ends `_retry_recovered`; redraft question rejected →
  `_retry_failed`.
- common_words module: no digits, no entries shorter than 3 chars, all
  lowercase (a hygiene test).

## Docs & git

- Update the leak-guard paragraph in `docs/architecture/apollo.md`
  (common-English allowance, morphology, accepted residual risk, retry-label
  semantics); `last_verified: 2026-07-15`.
- Copy this spec into the branch as
  `docs/_archive/specs/2026-07-15-apollo-leak-guard-relaxation-spec.md`.
- Conventional commits ON THE EXISTING BRANCH `fix/apollo-question-dedup-retry`
  in the worktree named above (verify with `git status` that it is clean and
  on that branch first). MUST commit. No push, no PR. Do not touch the main
  ai-ta-backend checkout.
- Gates: pytest (smart_questions + chat clarification suites), ruff, mypy,
  diff-cover --compare-branch=origin/staging --fail-under=95. Report
  verbatim outputs + SHAs.
