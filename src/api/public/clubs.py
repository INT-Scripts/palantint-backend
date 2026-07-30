import uuid
from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.common import CLUB_KINDS, build_foyer_index, club_type
from db.database import get_db
from db.models import Event, EventOrganization, Organization

router = APIRouter(tags=["clubs"])


def _serialize_club_list_item(club: Organization, foyer_room: str | None) -> Dict[str, Any]:
    return {
        "id": str(club.id),
        "name": club.name,
        "slug": club.slug,
        "description": club.description,
        "logo_url": club.logo_url,
        "type": club_type(club),
        "association_of_origin": club.attributes.get("association_of_origin"),
        "color_primary": club.color_primary,
        "color_secondary": club.color_secondary,
        "foyer_room": foyer_room,
    }


@router.get("/clubs")
async def get_clubs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Organization).where(Organization.kind.in_(CLUB_KINDS)))
    clubs = result.scalars().all()
    _, room_id_by_club_id = await build_foyer_index(db)
    return [_serialize_club_list_item(c, room_id_by_club_id.get(c.id)) for c in clubs]


@router.get("/foyer/map", response_model=Dict[str, Any])
async def get_foyer_map(db: AsyncSession = Depends(get_db)):
    """
    Returns full room mapping for Foyer Floor 0 and Floor 1,
    linking each room_id (F0-1 to F1-16) with database Organization (club) entities.
    """
    rooms_by_id, _ = await build_foyer_index(db)
    if not rooms_by_id:
        raise HTTPException(status_code=500, detail="Foyer map data source not found")
    return rooms_by_id


@router.get("/clubs/{club_id}")
async def get_club_details(
    club_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Organization)
        .options(
            selectinload(Organization.links),
        )
        .where(Organization.id == club_id, Organization.kind.in_(CLUB_KINDS))
    )
    club = result.scalars().first()
    if not club:
        raise HTTPException(status_code=404, detail="Club not found")

    _, room_id_by_club_id = await build_foyer_index(db)
    foyer_room_code = room_id_by_club_id.get(club.id)

    events_result = await db.execute(
        select(Event)
        .options(selectinload(Event.location))
        .join(EventOrganization, EventOrganization.event_id == Event.id)
        .where(EventOrganization.organization_id == club.id)
    )
    club_events = events_result.scalars().all()
    if not club_events:
        events_result = await db.execute(
            select(Event).options(selectinload(Event.location)).where(Event.organization_id == club.id)
        )
        club_events = events_result.scalars().all()

    return {
        "id": str(club.id),
        "name": club.name,
        "description": club.description,
        "logo_url": club.logo_url,
        "type": club_type(club),
        "association_of_origin": club.attributes.get("association_of_origin"),
        "color_primary": club.color_primary,
        "color_secondary": club.color_secondary,
        "foyer_room": foyer_room_code,
        "slug": club.slug,
        "members": [],  # stripped for public safety
        "links": [{"name": link.name, "url": link.url} for link in club.links] if club.links else [],
        "events": [
            {
                "id": str(e.id),
                "name": e.name,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "room": (e.location.name or e.location.code) if e.location else None,
                "description": e.description,
            }
            for e in sorted(club_events, key=lambda ev: ev.start_time)
        ],
    }
