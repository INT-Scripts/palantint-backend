"""Public course catalog (INT Portal).

The catalog is scraped from the three schools' public "enseignements" sites,
so it carries no privileged data and is served without authentication.

Two things the private router does are deliberately *not* done here: teacher
names are never resolved to Person profiles (the identity graph stays in the
operator space), and no course sheet exposes anything beyond what the official
catalog already publishes.
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
    serialize_course_details,
    serialize_course_summary,
)
from db.database import get_db
from db.models import Course

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("/filters")
async def get_course_filters(db: AsyncSession = Depends(get_db)):
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
):
    result = await db.execute(
        select(Course).options(selectinload(Course.teachers)).where(Course.id == course_id)
    )
    course = result.scalars().first()
    if not course or not course.is_active:
        raise HTTPException(status_code=404, detail="Course not found")

    # No `people_by_key`: names as published, never a link into the directory.
    return serialize_course_details(course)
