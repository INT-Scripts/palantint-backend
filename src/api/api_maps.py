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

class ThreeDConfigSchema(BaseModel):
    tile_mappings: Dict[str, str] = {}
    markers: List[Dict[str, Any]] = []

@router.get("/3d-config", response_model=ThreeDConfigSchema)
async def get_3d_config():
    """Returns the configuration for 3D tile mappings and markers."""
    config_path = "/app/assets/3d/config.json"
    if os.path.exists(config_path):
        import json
        with open(config_path, 'r') as f:
            return ThreeDConfigSchema(**json.load(f))
    return ThreeDConfigSchema()

@router.post("/3d-config")
async def save_3d_config(
    config: ThreeDConfigSchema,
    current_admin: User = Depends(get_current_admin_user)
):
    """Saves the configuration for 3D tile mappings and markers."""
    config_path = "/app/assets/3d/config.json"
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    import json
    with open(config_path, 'w') as f:
        json.dump(config.model_dump(), f, indent=4)
    return {"status": "success"}

@router.get("/3d-tiles")
async def get_3d_tiles():
    """Returns a list of available 3D tile GLTF files."""
    tiles_dir = "/app/assets/3d"
    if not os.path.exists(tiles_dir):
        return {"tiles": []}
    
    # List all .gltf files and sort them to maintain order
    files = [f for f in os.listdir(tiles_dir) if f.endswith(".gltf")]
    # Try to sort logically if possible (e.g. tile_1, tile_2...)
    try:
        files.sort(key=lambda x: int(x.replace('tile_', '').replace('.gltf', '')))
    except Exception:
        files.sort()
        
    # Return full paths as accessed via the static mount
    urls = [f"/api/assets/3d/{f}" for f in files]
    return {"tiles": urls}

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
