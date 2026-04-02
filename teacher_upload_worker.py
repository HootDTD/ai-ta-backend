from __future__ import annotations

"""Background worker for queued teacher upload ingestion."""

import logging

from knowledge.teacher_weekly import TeacherWeeklyStorage


log = logging.getLogger("teacher_upload_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    storage = TeacherWeeklyStorage()
    storage.run_upload_worker_loop()


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    main()
