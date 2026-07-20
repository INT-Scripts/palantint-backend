import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from db.database import get_db
from db.models import Club

router = APIRouter(tags=["clubs"])


@router.get("/clubs")
async def get_clubs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Club))
    return result.scalars().all()


@router.get("/clubs/{club_id}")
async def get_club_details(
    club_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Club)
        .options(
            selectinload(Club.events),
            selectinload(Club.links),
        )
        .where(Club.id == club_id)
    )
    club = result.scalars().first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")

    return {
        "id": str(club.id),
        "name": club.name,
        "description": club.description,
        "logo_url": club.logo_url,
        "type": club.type,
        "association_of_origin": club.association_of_origin,
        "color_primary": club.color_primary,
        "color_secondary": club.color_secondary,
        "slug": club.slug,
        "members": [],  # stripped for public safety
        "links": [{"name": link.name, "url": link.url} for link in club.links] if club.links else [],
        "events": [
            {
                "id": str(e.id),
                "name": e.name,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "room": e.room,
                "description": e.description,
            }
            for e in sorted(club.events, key=lambda ev: ev.start_time)
        ]
        if club.events
        else [],
    }
