"""F1c ad-hoc bootstrap: recreate the search-space + teacher/student identity
fixtures that `reset_all()` wipes along with the schema drop. These rows are
NOT produced by any migration or seed script -- they were created ad hoc for
F1a/F1b and never preserved as a script. This is the reconstruction for the
F1c post-fix re-run.

Creates:
  - `aita_search_spaces` row, slug=campaign-course (id likely 1 again, since
    the schema was just dropped and autoincrement resets with the schema).
  - Supabase auth user for the teacher (admin API, same idempotent
    create-then-signin pattern as campaign.cast.student.mint_student_token)
    + a `course_memberships` row role="teacher".
  - Supabase auth user for a bootstrap "smoke" student is NOT created here --
    the corpus driver mints one student per persona itself and relies on
    AUTO_ENROLL_STUDENT_MEMBERSHIP (default-on) for role="student" rows.

Usage (anaconda/base interpreter -- no torch needed):
    python campaign/out/f1c/bootstrap_course.py
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
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from database.models import CourseMembership, SearchSpace  # noqa: E402

TEACHER_EMAIL = "campaign-teacher@example.com"
TEACHER_PASSWORD = "CampaignTeacher123!"


async def mint_teacher(supabase_url: str, service_role_key: str) -> tuple[str, str]:
    """Create (idempotent) the teacher auth user; return (user_id, access_token)."""
    async with httpx.AsyncClient(base_url=supabase_url, timeout=30.0) as client:
        headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        create_resp = await client.post(
            "/auth/v1/admin/users",
            json={"email": TEACHER_EMAIL, "password": TEACHER_PASSWORD, "email_confirm": True},
            headers=headers,
        )
        # NOTE (bug found + worked around live during F1c): the admin
        # list-users-by-email fallback below does NOT reliably filter by
        # email on this GoTrue version -- it silently returned a DIFFERENT
        # user's id at index 0 once the teacher already existed, producing a
        # course_memberships row keyed to the wrong auth id. The JWT's own
        # "sub" claim (decoded, not the admin-API lookup) is the only
        # trustworthy source of the real signed-in user id -- always derive
        # user_id from the minted token, never from the admin list response.
        del create_resp  # response body's "id" is not trusted either; see above

        signin_resp = await client.post(
            "/auth/v1/token?grant_type=password",
            json={"email": TEACHER_EMAIL, "password": TEACHER_PASSWORD},
            headers={"apikey": service_role_key},
        )
        signin_resp.raise_for_status()
        token = signin_resp.json()["access_token"]

        import base64
        import json as _json

        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        user_id = _json.loads(base64.urlsafe_b64decode(payload_b64))["sub"]
        return user_id, token


async def main() -> None:
    supabase_url = os.environ["SUPABASE_URL"]
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    dsn = os.environ["SUPABASE_DB_URL"]

    engine = create_async_engine(dsn, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    teacher_id, teacher_token = await mint_teacher(supabase_url, service_role_key)

    async with Session() as db:
        space = SearchSpace(name="Campaign Course", slug="campaign-course", subject_name="Campaign")
        db.add(space)
        await db.flush()
        search_space_id = space.id

        db.add(
            CourseMembership(user_id=teacher_id, search_space_id=search_space_id, role="teacher")
        )
        await db.commit()

    out = {
        "search_space_id": search_space_id,
        "teacher_user_id": teacher_id,
        "teacher_email": TEACHER_EMAIL,
        "teacher_password": TEACHER_PASSWORD,
        "teacher_token": teacher_token,
    }
    print(json.dumps(out, indent=2))
    (Path(__file__).resolve().parent / "course-bootstrap.json").write_text(
        json.dumps(out, indent=2)
    )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
