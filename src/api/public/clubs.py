import csv
import os
import re
import uuid
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from core.config import settings
from db.database import get_db
from db.models import Club

router = APIRouter(tags=["clubs"])

# data/scraps/manual (the source of truth, regenerated via
# scripts/src/palantint_scripts/map_gen.py) is NOT mounted into the backend
# container — only data/assets is (see compose.yaml). Read the CSV copy that
# map_gen.py exports alongside foyer_map.json instead.
DATA_DIR = str(settings.ASSETS_DIR / "clubs")


def _extract_candidate_names(raw_name: str) -> List[str]:
    """
    Some foyer rooms are shared by several clubs, encoded either as a
    parenthetical list ("Cave (Club Code, ModelIT, GamINT, CELL)") or as a
    bare comma-separated list ("PaintIT,TellTheTale,INTimes"). Others use
    the parens for a location detail on a single club ("Minet (Bagagerie)").
    Returns the list of candidate club names to try matching against the DB.
    """
    raw_name = raw_name.strip()
    if not raw_name:
        return []

    match = re.match(r"^(.*?)\((.*)\)\s*$", raw_name)
    if match:
        base, inner = match.group(1).strip(), match.group(2).strip()
        if "," in inner:
            return [t.strip() for t in inner.split(",") if t.strip()]
        return [base] if base else [inner]

    if "," in raw_name:
        return [t.strip() for t in raw_name.split(",") if t.strip()]

    return [raw_name]


def _serialize_club(club: Club) -> Dict[str, Any]:
    return {
        "club_id": str(club.id),
        "club_name": club.name,
        "logo_url": club.logo_url,
        "description": club.description,
        "type": club.type,
        "association_of_origin": club.association_of_origin,
    }


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
    if not os.path.exists(csv_path):
        raise HTTPException(status_code=500, detail="Foyer map data source not found")

    foyer_entries = []
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

        matched_clubs: List[Club] = []
        foyer_match = clubs_by_foyer.get(room_id)
        if foyer_match:
            matched_clubs.append(foyer_match)

        for candidate in _extract_candidate_names(raw_name):
            candidate_lower = candidate.lower()
            club_obj: Optional[Club] = clubs_by_name.get(candidate_lower)
            if not club_obj:
                for db_name_lower, c in clubs_by_name.items():
                    if candidate_lower and (candidate_lower in db_name_lower or db_name_lower in candidate_lower):
                        club_obj = c
                        break
            if club_obj and club_obj.id not in {c.id for c in matched_clubs}:
                matched_clubs.append(club_obj)

        primary = matched_clubs[0] if matched_clubs else None

        output[room_id] = {
            "room_id": room_id,
            "raw_name": raw_name,
            "club_name": primary.name if primary else raw_name,
            "club_id": str(primary.id) if primary else None,
            "logo_url": primary.logo_url if primary else None,
            "description": primary.description if primary else None,
            "type": primary.type if primary else "Club",
            "association_of_origin": primary.association_of_origin if primary else None,
            "floor": floor,
            "building": "Foyer",
            "clubs": [_serialize_club(c) for c in matched_clubs],
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
