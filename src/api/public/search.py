from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.private.deps import escape_like
from db.database import get_db
from db.models import Organization

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
async def global_search(
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    if not q or len(q) < 2:
        return {"students": [], "clubs": [], "class_groups": [], "apartments": []}

    query_clean = q.strip().lower()

    club_query = select(Organization).where(
        Organization.kind.in_(("CLUB", "BUREAU")),
        or_(
            func.unaccent(Organization.name).ilike(func.unaccent(f"%{escape_like(query_clean)}%")),
            func.unaccent(Organization.slug).ilike(f"%{escape_like(query_clean)}%")
        )
    ).limit(10)
    club_res = await db.execute(club_query)
    clubs_raw = club_res.scalars().all()

    ranked_clubs = []
    for c in clubs_raw:
        score = 0
        name = (c.name or "").lower()
        slug = (c.slug or "").lower()
        if name == query_clean:
            score += 100
        if slug == query_clean:
            score += 90
        if name.startswith(query_clean):
            score += 50
        ranked_clubs.append((score, c))
    ranked_clubs.sort(key=lambda x: x[0], reverse=True)

    return {
        "students": [],
        "clubs": [
            {
                "id": str(c.id),
                "name": c.name,
                "slug": c.slug,
                "logo_url": c.logo_url
            } for score, c in ranked_clubs[:5]
        ],
        "class_groups": [],
        "apartments": []
    }
