---
doc: platform/worker-and-procfile
description: Procfile + teacher_upload_worker.py — the Railway two-process split (web API vs background upload-ingestion worker)
owns:
  - teacher_upload_worker.py
  - Procfile
related:
  - platform/http-server
  - knowledge/teacher-weekly
last_verified: 2026-08-04
stub: false
---

# platform/worker-and-procfile — the two-process split

Railway runs two processes from one image; this doc owns the wiring, not the
ingestion logic.

## Interface

- `Procfile` declares:
  - `web: uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers
    ${WEB_CONCURRENCY:-4}` — the API, now multiple worker processes (default 4;
    tunable via `WEB_CONCURRENCY`), each its own event loop — one hung
    synchronous call inside a request handler no longer freezes every
    concurrent Apollo turn on the replica (2026-08-04, `perf/apollo-event-loop-concurrency`).
  - `worker: python -m teacher_upload_worker` — the ingestion drainer.
- `teacher_upload_worker.py::main()` — configures root logging, constructs a
  `TeacherWeeklyStorage()`, and calls `run_upload_worker_loop()`; guarded by
  `if __name__ == "__main__"`.

## Data flow

The API answers questions and enqueues teacher uploads off the request path
(`POST /teacher/upload` in `http-server`); the worker process drains that queue
by looping in `TeacherWeeklyStorage.run_upload_worker_loop()` (OCR + index),
owned by `knowledge/teacher-weekly`.

## Invariants & gotchas

- The two processes share the image but not the request lifecycle — ingestion
  never blocks `/ask`.
- This is a thin entrypoint only: the actual OCR/index loop belongs to
  `knowledge/teacher-weekly`. The dormant upload-triggered Apollo provisioning
  worker was removed (cleanup T-F).

## Related

`http-server` (the enqueue side), `knowledge/teacher-weekly` (the loop body).
