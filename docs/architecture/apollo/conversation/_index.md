---
doc: apollo/conversation/_index
description: Router + Apollo teaching-turn end-to-end authority — routing, handlers, agent, parser, questioning, curriculum, session entry
owns: []
related: []
last_verified: 2026-07-27
stub: false
---

# Apollo conversation — the teaching-turn path

Live path: `api.py` (routing/router) → session_init → chat (parser + questioning) → done (grade). Neo4j is optional; the transcript LLM grader is the sole grading lane. The questioning/ leaves own `apollo/smart_questions/` (renamed in the doc tree, not on disk).

Grading-path recipe (D21): to change grading, start at handlers/done (the orchestrator) and follow its directional related chain; the full recipe + grading invariants live in [overseer/_index](../overseer/_index.md).

## Cross-cutting invariants
- The transcript adjudicator is the ONLY grading lane; a grading failure returns a retryable 503, never a fallback grade.
- `history_summary` / `history_summary_up_to_turn` are legacy resets only — never populated live (the windowed-history path is vestigial).
- routing/errors is an apollo-WIDE taxonomy; the Neo4j singleton + exception registration in routing/router serve every apollo sub-domain.

## Routing
| Leaf | Role · owns |
|---|---|
| [router](routing/router.md) | /apollo APIRouter + Neo4j singleton + exception registration · apollo/api.py |
| [errors](routing/errors.md) | NO-FALLBACK exception taxonomy (apollo-wide) · apollo/errors.py |
| [auth-deps](routing/auth-deps.md) | 4 async auth deps + DB-08b RLS ordering · apollo/auth_deps.py |

## Handlers — live
| Leaf | Role · owns |
|---|---|
| [chat](handlers/chat.md) | handle_chat full V3 teaching turn · handlers/chat.py |
| [done](handlers/done.md) | handle_done grade-of-record ORCHESTRATOR · handlers/done.py |
| [grading-artifact-writer](handlers/grading-artifact-writer.md) | write_artifacts canonical GradingRun row · handlers/artifact_writer.py |
| [negotiate](handlers/negotiate.md) | P3 challenge/paraphrase/skip/trace · handlers/negotiate.py |
| [intent](handlers/intent.md) | chat intent classifier + confirmation gate · handlers/intent.py |
| [lifecycle](handlers/lifecycle.md) | retry/end/get-session snapshot · handlers/lifecycle.py |
| [navigation](handlers/navigation.md) | next + restart_problem transitions · handlers/{next,restart_problem}.py |
| [browse](handlers/browse.md) | read-only problem browse · handlers/browse.py |
| [progress](handlers/progress.md) | course-scoped XP/level read · handlers/progress.py |

## Handlers — vestigial (deletion candidates)
| Leaf | Role |
|---|---|
| [history](handlers/history.md) | [V] dead windowed-history loader |
| [olm-invite](handlers/olm-invite.md) | [V] dead P3.5 clarification-invite |
| [done-turn-order](handlers/done-turn-order.md) | [V] dead WU-4C1 shadow turn order |

## Agent · parser
| Leaf | Role · owns |
|---|---|
| [llm-client](agent/llm-client.md) | cheap_chat/main_chat tiers (cross-domain) · agent/_llm.py |
| [persona-reply](agent/persona-reply.md) | [V] dead draft_reply + ContextOverflowError · agent/apollo_llm.py |
| [output-filter](agent/output-filter.md) | [V] dead 2-stage leakage barrier · agent/{output_filter,leakage_judge}.py |
| [parser-llm](parser/parser-llm.md) | parse_utterance live entry · parser/parser_llm.py + prompt_builder.py |
| [extraction-schema](parser/extraction-schema.md) | build_extraction_schema output contract · parser/extraction_schema.py |
| [edge-resolver](parser/edge-resolver.md) | resolve_typed_edges endpoint rules · parser/edge_resolver.py |
| [graph-context](parser/graph-context.md) | build_graph_context prior-attempt context · parser/graph_context.py |

## Questioning · curriculum · session entry
| Leaf | Role · owns |
|---|---|
| [unified](questioning/unified.md) | evaluate_and_ask one-call engine + log-only belt · smart_questions/unified.py |
| [controller](questioning/controller.md) | plan_next_question persistence orchestration · smart_questions/controller.py |
| [registry](curriculum/registry.md) | filesystem authoring registry · apollo/subjects/__init__.py |
| [db](curriculum/db.md) | live DB-backed concept loader · apollo/subjects/curriculum_db.py |
| [session-init](session-init.md) | hoot_bridge session creation (both entry paths) · hoot_bridge/session_init.py |
| [hoot-bridge-reference-answer](hoot-bridge-reference-answer.md) | Explicit `ask_hoot` INTERACTION4 hint-lane bridge · hoot_bridge/reference_answer.py |
