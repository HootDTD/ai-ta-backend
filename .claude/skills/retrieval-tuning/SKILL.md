---
name: retrieval-tuning
description: Triggered when modifying anything in the retrieval pipeline — hybrid search, reranking, context packing, or store bias.
user-invocable: true
---

## Rules
- Never change hybrid search fusion logic without running the full retrieval test suite first
- Any change to ranking weights must be documented in config/weights.py with a comment explaining why
- Test with at least 3 different query types: factual, conceptual, and equation-based
- Log before/after retrieval scores when testing changes
- Changes to context_packer.py must preserve citation marker generation — this is non-negotiable
