import json
import os
from typing import Dict, List, Any, Tuple
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.config import settings
from db.database import get_db
from db.models import Location, MapMetadata, ThreeDConfig

router = APIRouter(prefix="/maps", tags=["public-maps"])


class Pillar(BaseModel):
    x: float
    y: float


class MapMetadataSchema(BaseModel):
    pillars: List[Pillar] = []


class BuildingCoordinates(BaseModel):
    lat: float = 0.0
    lng: float = 0.0


class BuildingDetails(BaseModel):
    # Public-safe subset only — no occupant/tenant data belongs here.
    address: str = ""
    coordinates: BuildingCoordinates = BuildingCoordinates()


class BuildingMarker(BaseModel):
    id: str = ""
    bldg_id: str
    label: str = ""
    footprint: List[Tuple[float, float, float]] = []
    details: BuildingDetails = BuildingDetails()


class ThreeDConfigSchema(BaseModel):
    tile_mappings: Dict[str, str] = {}
    markers: List[BuildingMarker] = []


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


async def _get_building_location(db: AsyncSession, building_id: str):
    result = await db.execute(
        select(Location).where(Location.kind == "BUILDING", Location.code == building_id)
    )
    return result.scalars().first()


@router.get("/{building_id}/{floor_id}/metadata", response_model=MapMetadataSchema)
async def get_public_map_metadata(
    building_id: str,
    floor_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get public structural pillar alignment metadata for a given floor."""
    building = await _get_building_location(db, building_id)
    if not building:
        return MapMetadataSchema()

    floor_result = await db.execute(
        select(Location).where(
            Location.kind == "FLOOR",
            Location.parent_id == building.id,
            Location.code == floor_id,
        )
    )
    floor = floor_result.scalars().first()
    if not floor:
        return MapMetadataSchema()

    stmt = select(MapMetadata).where(MapMetadata.location_id == floor.id)
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
    building = await _get_building_location(db, building_id)
    if not building:
        return {}

    floors_result = await db.execute(
        select(Location).where(Location.kind == "FLOOR", Location.parent_id == building.id)
    )
    floors = {f.id: f.code for f in floors_result.scalars().all()}
    if not floors:
        return {}

    metas_result = await db.execute(
        select(MapMetadata).where(MapMetadata.location_id.in_(floors.keys()))
    )
    metas = metas_result.scalars().all()

    results = {}
    for m in metas:
        results[floors[m.location_id]] = {
            "pillars": m.pillars
        }
    return results


@router.get("/3d-tiles")
async def get_public_3d_tiles():
    """Public list of scanned campus tile GLTFs. The files themselves are
    already served without auth via the /assets static mount — this endpoint
    just discovers and orders them, mirroring api/private/maps.py's version."""
    tiles_dir = settings.ASSETS_DIR / "3d"
    if not os.path.exists(tiles_dir):
        return {"tiles": []}

    files = [f for f in os.listdir(tiles_dir) if f.endswith(".gltf")]
    try:
        files.sort(key=lambda x: int(x.replace('tile_', '').replace('.gltf', '')))
    except Exception:
        files.sort()

    urls = [f"/api/assets/3d/{f}" for f in files]
    return {"tiles": urls}


@router.get("/3d-config", response_model=ThreeDConfigSchema)
async def get_public_3d_config(db: AsyncSession = Depends(get_db)):
    """Public-safe projection of the 3D map config: tile mappings and building
    footprints/addresses only — no wireframe transforms or occupant data,
    since those are only meaningful together with the private floor/occupant
    endpoints."""
    stmt = select(ThreeDConfig).where(ThreeDConfig.key == "default")
    res = await db.execute(stmt)
    cfg = res.scalars().first()
    if cfg and (cfg.tile_mappings or cfg.markers):
        return ThreeDConfigSchema(tile_mappings=cfg.tile_mappings, markers=cfg.markers)

    export_path = settings.DATA_ROOT / "exports" / "3d_config.json"
    if os.path.exists(export_path):
        with open(export_path, 'r', encoding='utf-8') as f:
            return ThreeDConfigSchema(**json.load(f))

    config_path = settings.ASSETS_DIR / "3d" / "config.json"
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return ThreeDConfigSchema(**json.load(f))

    return ThreeDConfigSchema()
