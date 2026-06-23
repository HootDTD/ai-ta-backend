"""Local end-to-end smoke driver: ensure a teacher user + course + membership,
sign in for a JWT, and upload the smoke PDF to /teacher/upload as a textbook.

Idempotent (safe to re-run). Talks to the LOCAL Supabase stack (GoTrue admin +
password sign-in) and the LOCAL Postgres (course/membership rows via the ORM
models), then POSTs to the local web process.

Run after dot-sourcing scripts/load_local_env.ps1 (so SUPABASE_* are set) and
after filling SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY in .env.local:

    . .\scripts\load_local_env.ps1
    python scripts\local_e2e_smoke.py

Env knobs (all optional): SMOKE_TEACHER_EMAIL, SMOKE_TEACHER_PASSWORD,
SMOKE_COURSE_SLUG, SMOKE_COURSE_NAME, SMOKE_SUBJECT, SMOKE_PDF, WEB_BASE_URL.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Make the repo root importable when run as `python scripts/local_e2e_smoke.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

TEACHER_EMAIL = os.getenv("SMOKE_TEACHER_EMAIL", "teacher.smoke@example.com")
TEACHER_PASSWORD = os.getenv("SMOKE_TEACHER_PASSWORD", "smoke-Password-123")
COURSE_SLUG = os.getenv("SMOKE_COURSE_SLUG", "smoke-fluids")
COURSE_NAME = os.getenv("SMOKE_COURSE_NAME", "Smoke Fluids 101")
SUBJECT_NAME = os.getenv("SMOKE_SUBJECT", "Fluid Mechanics")
PDF_PATH = os.getenv("SMOKE_PDF", "scripts/smoke_bernoulli.pdf")


def _need(name: str) -> str:
    val = (os.getenv(name) or "").strip()
    if not val or val.startswith("PASTE_"):
        sys.exit(
            f"Missing env {name}. Dot-source scripts/load_local_env.ps1 and fill "
            f"{name} in .env.local (from `supabase status`)."
        )
    return val


def ensure_teacher_user(supabase_url: str, service_key: str) -> str:
    """Create (or find) the teacher auth user via the GoTrue admin API."""
    admin_headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{supabase_url}/auth/v1/admin/users",
        headers=admin_headers,
        json={"email": TEACHER_EMAIL, "password": TEACHER_PASSWORD, "email_confirm": True},
        timeout=30,
    )
    if resp.status_code in (200, 201):
        uid = resp.json()["id"]
        print(f"  created teacher user {TEACHER_EMAIL} ({uid})")
        return uid
    if resp.status_code in (400, 409, 422) or "already" in resp.text.lower():
        listing = requests.get(
            f"{supabase_url}/auth/v1/admin/users",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            timeout=30,
        )
        listing.raise_for_status()
        body = listing.json()
        users = body.get("users", body if isinstance(body, list) else [])
        for user in users:
            if user.get("email") == TEACHER_EMAIL:
                print(f"  teacher user already exists ({user['id']})")
                return user["id"]
    sys.exit(f"could not create/find teacher user: {resp.status_code} {resp.text}")


async def ensure_course_and_membership(db_url: str, user_id: str) -> int:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from database.models import CourseMembership, SearchSpace

    engine = create_async_engine(db_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            space = (
                await session.execute(select(SearchSpace).where(SearchSpace.slug == COURSE_SLUG))
            ).scalar_one_or_none()
            if space is None:
                space = SearchSpace(name=COURSE_NAME, slug=COURSE_SLUG, subject_name=SUBJECT_NAME)
                session.add(space)
                await session.flush()
                print(f"  created course '{COURSE_NAME}' (search_space_id={space.id})")
            else:
                print(f"  course already exists (search_space_id={space.id})")
            space_id = space.id

            membership = (
                await session.execute(
                    select(CourseMembership).where(
                        CourseMembership.user_id == user_id,
                        CourseMembership.search_space_id == space_id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                session.add(
                    CourseMembership(user_id=user_id, search_space_id=space_id, role="teacher")
                )
                print("  added teacher membership")
            elif membership.role != "teacher":
                membership.role = "teacher"
                print("  upgraded membership to teacher")
            else:
                print("  teacher membership already present")
            await session.commit()
            return space_id
    finally:
        await engine.dispose()


def sign_in(supabase_url: str, anon_key: str) -> str:
    resp = requests.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"email": TEACHER_EMAIL, "password": TEACHER_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    print("  signed in, obtained access token")
    return resp.json()["access_token"]


def upload(web_base: str, token: str, search_space_id: int) -> None:
    with open(PDF_PATH, "rb") as handle:
        resp = requests.post(
            f"{web_base}/teacher/upload",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "search_space_id": str(search_space_id),
                "week": "0",
                "kind": "textbook",
                "title": "Bernoulli smoke",
            },
            files={"file": ("smoke_bernoulli.pdf", handle, "application/pdf")},
            timeout=120,
        )
    print(f"  upload -> HTTP {resp.status_code}: {resp.text[:400]}")
    resp.raise_for_status()


def main() -> None:
    supabase_url = _need("SUPABASE_URL")
    anon_key = _need("SUPABASE_ANON_KEY")
    service_key = _need("SUPABASE_SERVICE_ROLE_KEY")
    db_url = _need("SUPABASE_DB_URL")
    web_base = os.getenv("WEB_BASE_URL", "http://127.0.0.1:8000")
    if not os.path.isfile(PDF_PATH):
        sys.exit(f"PDF not found: {PDF_PATH} (run: python scripts/make_smoke_pdf.py)")

    print("1) ensure teacher user")
    user_id = ensure_teacher_user(supabase_url, service_key)
    print("2) ensure course + membership")
    space_id = asyncio.run(ensure_course_and_membership(db_url, user_id))
    print("3) sign in")
    token = sign_in(supabase_url, anon_key)
    print("4) upload textbook")
    upload(web_base, token, space_id)

    print(f"\nDONE. search_space_id={space_id}, teacher={TEACHER_EMAIL}")
    print("Now watch the `worker` then `apollo-provision` logs. When they settle:")
    print("  select tier, count(*) from apollo_concept_problems group by tier;")
    print("  -- expect tier=2 rows = the auto-provisioned question bank.")


if __name__ == "__main__":
    main()
