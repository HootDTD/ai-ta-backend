"""Campaign cast — teacher-side provisioning drivers (Phase D, Task D1).

Two provisioning shapes feed the campaign's subject registry:

* **seeded incumbents** — subjects already on disk under ``apollo/subjects/``
  (``fluid_mechanics``, ``macroeconomics``), provisioned by replaying the
  existing filesystem-registry seeding scripts against the campaign's local
  DB (no HTTP, no LLM).
* **WU-AAS authored** — the REAL teacher-facing path: a problem+solution PDF
  pair uploaded through ``POST /apollo/authored-sets``, polled to completion,
  then any held-for-review problems approved through the approve endpoint.

See ``campaign/cast/subjects.py`` for the registry and
``campaign/cast/teacher.py`` for both drivers.
"""

from __future__ import annotations
