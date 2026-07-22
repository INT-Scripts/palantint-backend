from typing import Dict, List, Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.database import get_db
from db.models import MapMetadata

router = APIRouter(prefix="/maps", tags=["public-maps"])


class Pillar(BaseModel):
    x: float
    y: float


class MapMetadataSchema(BaseModel):
    pillars: List[Pillar] = []


BUILDING_FLOORS: Dict[str, List[str]] = {
    "Foyer": ["0", "1"],
    "U1": ["0", "1", "2", "3", "4", "5"],
    "U2": ["1", "2", "3", "4", "5"],
    "U3": ["0", "1", "2"],
    "U4": ["1", "2", "3", "4", "5", "6"],
    "U5": ["-0.5", "0.5", "1", "2", "3", "4"],
    "U6": ["1", "2", "3"],
    "U7": ["1", "2", "3", "4", "5", "6"],
}


@router.get("/buildings", response_model=Dict[str, List[str]])
async def get_public_buildings():
    """Return available residential buildings and their floor levels."""
    return BUILDING_FLOORS


@router.get("/{building_id}/{floor_id}/metadata", response_model=MapMetadataSchema)
async def get_public_map_metadata(
    building_id: str,
    floor_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get public structural pillar alignment metadata for a given floor."""
    stmt = select(MapMetadata).where(
        MapMetadata.building_id == building_id,
        MapMetadata.floor_id == floor_id
    )
    result = await db.execute(stmt)
    meta = result.scalars().first()
    if meta and meta.pillars:
        return MapMetadataSchema(
            pillars=[Pillar(**p) for p in meta.pillars]
        )
    return MapMetadataSchema()


@router.get("/{building_id}/metadata", response_model=Dict[str, Any])
async def get_public_building_metadata(
    building_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get public structural pillar metadata for all floors of a building."""
    stmt = select(MapMetadata).where(MapMetadata.building_id == building_id)
    result = await db.execute(stmt)
    metas = result.scalars().all()
    
    results = {}
    for m in metas:
        results[m.floor_id] = {
            "pillars": m.pillars
        }
    return results
