import os
from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert
from pydantic import BaseModel

from db.database import get_db
from db.models import MapMetadata
from api.routes import get_current_admin_user, User

router = APIRouter(prefix="/maps", tags=["maps"])

class Pillar(BaseModel):
    x: float
    y: float

class MapMetadataSchema(BaseModel):
    pillars: List[Pillar] = []

@router.get("/{building_id}/{floor_id}/metadata", response_model=MapMetadataSchema)
async def get_map_metadata(building_id: str, floor_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(MapMetadata).where(
        MapMetadata.building_id == building_id,
        MapMetadata.floor_id == floor_id
    )
    result = await db.execute(stmt)
    meta = result.scalars().first()
    if meta:
        return MapMetadataSchema(
            pillars=[Pillar(**p) for p in meta.pillars]
        )
    return MapMetadataSchema()

@router.post("/{building_id}/{floor_id}/metadata")
async def save_map_metadata(
    building_id: str, 
    floor_id: str, 
    metadata: MapMetadataSchema,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user)
):
    pillars_data = [p.model_dump() for p in metadata.pillars]
    
    stmt = insert(MapMetadata).values(
        building_id=building_id,
        floor_id=floor_id,
        pillars=pillars_data
    )
    
    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=["building_id", "floor_id"],
        set_={
            "pillars": stmt.excluded.pillars
        }
    )
    
    await db.execute(upsert_stmt)
    await db.commit()
    return {"status": "success"}

@router.get("/{building_id}/metadata")
async def get_building_metadata(building_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(MapMetadata).where(MapMetadata.building_id == building_id)
    result = await db.execute(stmt)
    metas = result.scalars().all()
    
    results = {}
    for m in metas:
        results[m.floor_id] = {
            "pillars": m.pillars
        }
    return results
