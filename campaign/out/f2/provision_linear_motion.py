"""F1c ad-hoc driver: provision linear_motion via the real WU-AAS
authored-sets upload path (POST /apollo/authored-sets -> poll -> approve),
mirroring the F1a provisioning (see campaign/out/f1/provisioning-notes.md).
The fixture PDFs (campaign/cast/materials/linear_motion_{problem,solution}.pdf)
already carry the F1a remediation (single '=' per equation, '**' not '^',
'd' not 'x'), so this may promote both problems in ONE upload where F1a
needed three attempts to get there incrementally.

Requires the backend to be UP at http://127.0.0.1:8000 (this hits the real
HTTP route, not a subprocess).

Usage (anaconda/base interpreter -- no torch needed, this is pure httpx):
    python campaign/out/f1c/provision_linear_motion.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env(REPO_ROOT / ".env.campaign")

import httpx  # noqa: E402

from campaign.cast.subjects import LINEAR_MOTION  # noqa: E402
from campaign.cast.teacher import AuthoredProvisioningError, provision_authored  # noqa: E402

BASE_URL = "http://127.0.0.1:8010"  # F2 isolated backend
OUT_DIR = Path(__file__).resolve().parent
TMP_DIR = Path(r"C:\Users\ultra\AppData\Local\Temp")


async def _upload_once(client: httpx.AsyncClient, token: str, search_space_id: int, tag: str):
    subj = LINEAR_MOTION.resolve()
    try:
        result = await provision_authored(
            client=client,
            base_url=BASE_URL,
            teacher_token=token,
            search_space_id=search_space_id,
            problem_pdf=subj.problem_pdf,
            solution_pdf=subj.solution_pdf,
        )
        print(
            f"[{tag}] set_id={result.set_id} status={result.status} "
            f"minted={result.minted_problem_ids} approved={result.approved_problem_ids} "
            f"failed={result.failed_approvals}"
        )
        return result
    except AuthoredProvisioningError as exc:
        print(f"[{tag}] AuthoredProvisioningError: {exc}")
        return None


async def _dump_status(client: httpx.AsyncClient, token: str, set_id: int, name: str) -> None:
    resp = await client.get(
        f"{BASE_URL}/apollo/authored-sets/{set_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    body = resp.json()
    (TMP_DIR / name).write_text(json.dumps(body, indent=2))
    (OUT_DIR / name).write_text(json.dumps(body, indent=2))
    print(
        f"dumped set {set_id} status -> {name}: {json.dumps(body.get('result_summary', {}), indent=2)[:1000]}"
    )


async def main() -> None:
    course = json.loads((OUT_DIR / "course-bootstrap.json").read_text())
    token = course["teacher_token"]
    search_space_id = course["search_space_id"]

    async with httpx.AsyncClient(timeout=120.0) as client:
        result = await _upload_once(client, token, search_space_id, "attempt1")
        if result is not None:
            await _dump_status(
                client, token, result.set_id, f"authored_set_final{result.set_id}.json"
            )

        if result is None or len(result.approved_problem_ids) < 2:
            print(
                "Not both problems promoted in attempt1 -- retrying once "
                "(dup-hash idempotency means a second upload of the SAME "
                "fixture typically resolves the still-missing half, mirroring "
                "F1a's set2/set3 sequence)."
            )
            result2 = await _upload_once(client, token, search_space_id, "attempt2")
            if result2 is not None:
                await _dump_status(
                    client, token, result2.set_id, f"authored_set_final{result2.set_id}.json"
                )


if __name__ == "__main__":
    asyncio.run(main())
