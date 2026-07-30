import json
import os
import uuid
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased

from api.private.deps import User, require_admin, require_user
from db.database import get_db
from db.models import Location, MapMetadata, PersonHousing, ThreeDConfig

from core.config import settings

router = APIRouter(prefix="/maps", tags=["maps"])


class Pillar(BaseModel):
    x: float
    y: float


class MapMetadataSchema(BaseModel):
    pillars: List[Pillar] = []


class WireframeTransform(BaseModel):
    position: Tuple[float, float, float] = (0, 0, 0)
    rotation: Tuple[float, float, float] = (0, 0, 0)
    scale: float = 1.0
    floor_height: float = 0.5


class BuildingCoordinates(BaseModel):
    lat: float = 0.0
    lng: float = 0.0


class BuildingDetails(BaseModel):
    address: str = ""
    coordinates: BuildingCoordinates = BuildingCoordinates()


class BuildingMarker(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    bldg_id: str
    label: str = ""
    footprint: List[Tuple[float, float, float]] = []
    wireframe: WireframeTransform = WireframeTransform()
    details: BuildingDetails = BuildingDetails()


class ThreeDConfigSchema(BaseModel):
    tile_mappings: Dict[str, str] = {}
    markers: List[BuildingMarker] = []


@router.get("/buildings")
async def get_buildings(current_user: User = Depends(require_user)):
    """Was missing from the private router entirely — mcp_server.py's
    get_map_location tool has been calling this path since before this
    schema migration and always 404ing."""
    from api.public.maps import BUILDING_FLOORS
    return BUILDING_FLOORS


async def _get_building_location(db: AsyncSession, building_id: str) -> Optional[Location]:
    result = await db.execute(
        select(Location).where(Location.kind == "BUILDING", Location.code == building_id)
    )
    return result.scalars().first()


async def _get_floor_location(db: AsyncSession, building_id: str, floor_id: str) -> Optional[Location]:
    building = await _get_building_location(db, building_id)
    if not building:
        return None
    result = await db.execute(
        select(Location).where(
            Location.kind == "FLOOR",
            Location.parent_id == building.id,
            Location.code == floor_id,
        )
    )
    return result.scalars().first()


async def _get_or_create_floor_location(db: AsyncSession, building_id: str, floor_id: str) -> Location:
    """Resolve the (building, floor) Location pair, creating either level on the
    fly if this is the first time an admin saves metadata for it. The old code
    had no such validation (it just wrote raw building_id/floor_id strings), so
    we preserve that "always succeeds" behaviour rather than 404ing."""
    building = await _get_building_location(db, building_id)
    if not building:
        building = Location(kind="BUILDING", code=building_id, name=building_id)
        db.add(building)
        await db.flush()

    result = await db.execute(
        select(Location).where(
            Location.kind == "FLOOR",
            Location.parent_id == building.id,
            Location.code == floor_id,
        )
    )
    floor = result.scalars().first()
    if not floor:
        floor = Location(kind="FLOOR", code=floor_id, name=floor_id, parent_id=building.id)
        db.add(floor)
        await db.flush()
    return floor


@router.get("/{building_id}/{floor_id}/metadata", response_model=MapMetadataSchema)
async def get_map_metadata(
    building_id: str,
    floor_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    floor = await _get_floor_location(db, building_id, floor_id)
    if not floor:
        return MapMetadataSchema()

    stmt = select(MapMetadata).where(MapMetadata.location_id == floor.id)
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
    current_admin: User = Depends(require_admin)
):
    pillars_data = [p.model_dump() for p in metadata.pillars]

    floor = await _get_or_create_floor_location(db, building_id, floor_id)

    stmt = insert(MapMetadata).values(
        location_id=floor.id,
        pillars=pillars_data
    )

    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=["location_id"],
        set_={
            "pillars": stmt.excluded.pillars
        }
    )

    await db.execute(upsert_stmt)
    await db.commit()
    return {"status": "success"}


@router.get("/3d-config", response_model=ThreeDConfigSchema)
async def get_3d_config(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    """Returns the configuration for 3D tile mappings and markers from DB or vault file."""
    stmt = select(ThreeDConfig).where(ThreeDConfig.key == "default")
    res = await db.execute(stmt)
    cfg = res.scalars().first()
    if cfg and (cfg.tile_mappings or cfg.markers):
        return ThreeDConfigSchema(
            tile_mappings=cfg.tile_mappings,
            markers=cfg.markers
        )

    # Fallback to vault file
    export_path = settings.DATA_ROOT / "exports" / "3d_config.json"
    if os.path.exists(export_path):
        with open(export_path, 'r', encoding='utf-8') as f:
            return ThreeDConfigSchema(**json.load(f))

    config_path = settings.ASSETS_DIR / "3d" / "config.json"
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return ThreeDConfigSchema(**json.load(f))

    return ThreeDConfigSchema()


@router.post("/3d-config")
async def save_3d_config(
    config: ThreeDConfigSchema,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Saves the configuration for 3D tile mappings and markers to DB and vault."""
    markers_data = [m.model_dump() for m in config.markers]

    stmt = select(ThreeDConfig).where(ThreeDConfig.key == "default")
    res = await db.execute(stmt)
    cfg = res.scalars().first()
    if not cfg:
        cfg = ThreeDConfig(
            key="default",
            tile_mappings=config.tile_mappings,
            markers=markers_data
        )
        db.add(cfg)
    else:
        cfg.tile_mappings = config.tile_mappings
        cfg.markers = markers_data

    await db.commit()

    export_dir = settings.DATA_ROOT / "exports"
    os.makedirs(export_dir, exist_ok=True)
    with open(export_dir / "3d_config.json", 'w', encoding='utf-8') as f:
        json.dump(config.model_dump(), f, indent=4, ensure_ascii=False)

    return {"status": "success"}


@router.get("/3d-tiles")
async def get_3d_tiles(
    current_user: User = Depends(require_user)
):
    """Returns a list of available 3D tile GLTF files."""
    tiles_dir = settings.ASSETS_DIR / "3d"
    if not os.path.exists(tiles_dir):
        return {"tiles": []}
    
    files = [f for f in os.listdir(tiles_dir) if f.endswith(".gltf")]
    try:
        files.sort(key=lambda x: int(x.replace('tile_', '').replace('.gltf', '')))
    except Exception:
        files.sort()
        
    urls = [f"/api/private/maps/3d-tiles/file/{f}" for f in files]
    return {"tiles": urls}


@router.get("/3d-tiles/file/{filename}")
async def serve_3d_tile_file(
    filename: str,
    current_user: User = Depends(require_user)
):
    """Securely serve GLTF/BIN/JSON assets for the 3D map under authentication."""
    # Prevent directory traversal
    safe_filename = os.path.basename(filename)
    if safe_filename != filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Enforce safe file extensions
    allowed_extensions = {".gltf", ".bin", ".json", ".png", ".jpg", ".jpeg", ".webp"}
    _, ext = os.path.splitext(filename)
    if ext.lower() not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Forbidden file extension")

    tiles_dir = settings.ASSETS_DIR / "3d"
    file_path = os.path.join(tiles_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path)


@router.get("/{building_id}/metadata")
async def get_building_metadata(
    building_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
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


@router.get("/{building_id}/occupants")
async def get_building_occupants(
    building_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    """Counts people currently housed in an apartment registered under this building.

    The ingestion pipeline (scripts/.../loaders/apartments.py) sets each
    APARTMENT Location's `parent_id` to its BUILDING Location, so that's the
    sole convention matched here. (Apartment codes are bare room numbers,
    e.g. "1101" — never building-prefixed — so a code-prefix fallback would
    never match real data; dropped to avoid a false sense of resilience.)
    """
    ParentLoc = aliased(Location)
    stmt = (
        select(func.count(func.distinct(PersonHousing.person_id)))
        .select_from(PersonHousing)
        .join(Location, Location.id == PersonHousing.location_id)
        .join(ParentLoc, ParentLoc.id == Location.parent_id)
        .where(
            PersonHousing.ended_at.is_(None),
            Location.kind == "APARTMENT",
            ParentLoc.code == building_id,
        )
    )
    result = await db.execute(stmt)
    return {"occupants": result.scalar() or 0}
