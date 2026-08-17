"""Course catalog browser — operator space (see IDEAS.md §"Course Catalog
Browser"). The public space serves the same catalog through
api/public/courses.py; the difference is here: teachers are resolved to their
Person profile so a course sheet links into the directory.

Read-only: the catalog is a scrape, so nothing here writes.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.common import (
    build_course_query,
    course_filter_facets,
    resolve_people_by_name_keys,
    serialize_course_details,
    serialize_course_summary,
)
from api.private.deps import User, require_user
from db.database import get_db
from db.models import Course

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("/filters")
async def get_course_filters(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    return await course_filter_facets(db)


@router.get("")
async def get_courses(
    skip: int = 0,
    limit: int = Query(default=30, le=200),
    q: Optional[str] = None,
    school: Optional[str] = None,
    niveau: Optional[str] = None,
    domaine: Optional[str] = None,
    langue: Optional[str] = None,
    periode: Optional[str] = None,
    lieu: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    query = build_course_query(q, school, niveau, domaine, langue, periode, lieu)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))

    result = await db.execute(
        query.options(selectinload(Course.teachers))
        .order_by(Course.title, Course.external_id)
        .offset(skip)
        .limit(limit)
    )

    return {
        "total": total or 0,
        "skip": skip,
        "limit": limit,
        "courses": [serialize_course_summary(c) for c in result.scalars().all()],
    }


@router.get("/{course_id}")
async def get_course_details(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(
        select(Course).options(selectinload(Course.teachers)).where(Course.id == course_id)
    )
    course = result.scalars().first()
    if not course or not course.is_active:
        raise HTTPException(status_code=404, detail="Course not found")

    # Resolved on every read, so a teacher added to the directory today shows
    # up as a link on a course scraped months ago.
    people_by_key = await resolve_people_by_name_keys(
        db, {t.name_key for t in course.teachers}
    )
    return serialize_course_details(course, people_by_key)
