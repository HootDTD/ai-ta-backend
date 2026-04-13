---
name: debug
description: Triggered when investigating a bug, error, or unexpected behavior.
user-invocable: true
---

## Process
1. Read the error message and stack trace in full before doing anything
2. Identify which pipeline stage is failing (retrieval, reranking, LLM, citation, vision)
3. Check the debug logs for that stage first — do not guess
4. Form a hypothesis and explain it to me before making any changes
5. Make one change at a time — never shotgun multiple fixes simultaneously
6. Run the relevant test module after each change to verify
7. Summarize what the root cause was and what was changed to fix it
