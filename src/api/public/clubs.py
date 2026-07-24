import csv
import os
import re
import uuid
from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from db.database import get_db
from db.models import Club

router = APIRouter(tags=["clubs"])

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps/auto"))


@router.get("/clubs")
async def get_clubs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Club))
    return result.scalars().all()


@router.get("/foyer/map", response_model=Dict[str, Any])
async def get_foyer_map(db: AsyncSession = Depends(get_db)):
    """
    Returns full room mapping for Foyer Floor 0 and Floor 1,
    linking each room_id (F0-1 to F1-16) with database Club entities.
    """
    csv_path = os.path.join(DATA_DIR, "foyer_map.csv")
    foyer_entries = []
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                room_id = row.get("room_id", "").strip()
                club_name = row.get("club_name", "").strip()
                if room_id:
                    floor = "0" if room_id.startswith("F0") else ("1" if room_id.startswith("F1") else "0")
                    foyer_entries.append({
                        "room_id": room_id,
                        "club_name": club_name,
                        "floor": floor
                    })

    # Query existing database clubs for matching
    res = await db.execute(select(Club))
    clubs_list = res.scalars().all()
    clubs_by_name = {c.name.lower(): c for c in clubs_list}
    clubs_by_foyer = {c.foyer_room: c for c in clubs_list if c.foyer_room}

    output = {}
    for entry in foyer_entries:
        room_id = entry["room_id"]
        raw_name = entry["club_name"]
        floor = entry["floor"]

        matched_club = clubs_by_foyer.get(room_id)
        if not matched_club and raw_name:
            clean_name = re.sub(r'\(.*?\)', '', raw_name).strip().lower()
            matched_club = clubs_by_name.get(raw_name.lower()) or clubs_by_name.get(clean_name)
            if not matched_club:
                for db_name_lower, club_obj in clubs_by_name.items():
                    if clean_name and (clean_name in db_name_lower or db_name_lower in clean_name):
                        matched_club = club_obj
                        break

        output[room_id] = {
            "room_id": room_id,
            "raw_name": raw_name,
            "club_name": matched_club.name if matched_club else raw_name,
            "club_id": str(matched_club.id) if matched_club else None,
            "logo_url": matched_club.logo_url if matched_club else None,
            "description": matched_club.description if matched_club else None,
            "type": matched_club.type if matched_club else "Club",
            "association_of_origin": matched_club.association_of_origin if matched_club else None,
            "floor": floor,
            "building": "Foyer"
        }

    return output


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
        "foyer_room": club.foyer_room,
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
