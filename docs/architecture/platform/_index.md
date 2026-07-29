---
doc: platform/_index
description: Router for the HTTP composition root, auth, request config, vendor clients, course workspaces, CI/CD, and one-shot ops scripts
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Platform — server, config, vendors, workspaces, ops

Reached from `shared-architecture/README`; descend to 1-3 leaves. `server.py`
(http-server) is the composition root that wires everything below into one
FastAPI app. The AI-use **reports** feature is a sibling domain
([reports/_index](../reports/_index.md)), not here — it depends on platform
auth + vendors but owns its own routes/models.

## Cross-cutting invariants
- **[config-contracts](config-contracts.md) is the single authority** on the QA
  dataclass shapes (`ResearchBundle`, `BundleSnippet`, …); every consuming doc
  links it, never restates fields. Changing a field ripples across the QA path.
- **[config-settings](config-settings.md) is the authority** on runtime env-flag
  getters (embedding/neo4j/reranker); other domains cite it.
- Retrieval **"weights"** are three distinct things: RRF rank-fusion has no
  per-arm weight (`rag-pipeline/hybrid-search`); post-fusion per-store-kind bias
  is `rag-pipeline/store-bias`; the bias-weight **config** is
  [config-weights](config-weights.md).
  The live teacher-tuning seam `server.py::_build_retrieval_weight_overrides`
  lives in [http-server](http-server.md) (§4.0.15 disambiguation).
- `server.py` **mounts three routers it does NOT own** (apollo, reports, chats);
  each router is owned by its own domain doc.

## HTTP surface
| Leaf | Role · owns |
|---|---|
| [http-server](http-server.md) | FastAPI composition root + QA HTTP surface · server.py |
| [auth](auth.md) | Supabase JWT + token cache + membership + auto-enroll · auth.py |
| [worker-and-procfile](worker-and-procfile.md) | web+worker two-process split · teacher_upload_worker.py + Procfile |

## Request config
| Leaf | Role · owns |
|---|---|
| [config-settings](config-settings.md) | RequestConfig + env-flag getters · config/settings.py |
| [config-contracts](config-contracts.md) | shared QA dataclasses · config/contracts.py |
| [config-weights](config-weights.md) | store-kind bias weights + clamp/normalize · config/weights.py |
| [config-model-pins](config-model-pins.md) | pinned solver model constants · config/models.py |

## Vendor clients · workspaces · CI/CD
| Leaf | Role · owns |
|---|---|
| [vendor-openai-client](vendor-openai-client.md) | reports-only Chat Completions wrapper · vendors/openai_client.py |
| [vendor-supabase-storage](vendor-supabase-storage.md) | Storage REST client · vendors/supabase_storage.py |
| [workspaces](workspaces.md) | course-workspace resolution + TTL cache · workspaces/{manager,db}.py |
| [ci-workflows](ci-workflows.md) | GitHub Actions + repo build-meta · .github/** + pytest.ini/ruff.toml/mypy.ini/… |

## Ops one-shot scripts (non-imported, run manually)
| Leaf | Role · owns |
|---|---|
| [ops-seed-scripts](ops-seed-scripts.md) | 4 idempotent Apollo/data seeder CLIs · scripts/seed_*.py |
| [ops-eval-scripts](ops-eval-scripts.md) | live-LLM eval/smoke/spike (never CI) · scripts/{eval,dag4,test_search,wave1}*, spikes/* |
| [ops-db-tooling](ops-db-tooling.md) | 4 Node DB dev/CI tools · scripts/db/*.mjs |
| [ops-db-sql](ops-db-sql.md) | 5 one-off #194-redesign SQL · scripts/db/*.sql |
