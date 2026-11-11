"""Private, provider-independent indoor plans in RFC 7946 GeoJSON."""

import hashlib
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.private.deps import require_user
from core.config import settings
from core.housing import apartment_details
from core.places import (
    mark_destinations,
    normalized_name,
    place_category,
    room_alias,
    room_codes,
    unpadded,
)
from db.database import get_db
from db.models import Event, Location, Organization, Person, PersonHousing
from services.plan_geography import (
    local_plans,
    plan_placements,
    projected_plans,
    superseded_places,
)

router = APIRouter(
    prefix="/maps/indoor", tags=["indoor-maps"], dependencies=[Depends(require_user)]
)


@lru_cache(maxsize=2)
def _read_collection(path, modified_ns, size):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("type") != "FeatureCollection":
        raise ValueError("Invalid indoor GeoJSON")
    return data


def revalidated(request, payload):
    """Large reference JSON: the browser keeps it and revalidates it, receiving 304 while it is unchanged."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    etag = f'"{hashlib.blake2b(body, digest_size=16).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def indoor_collection():
    path = settings.PRIVATE_ASSETS_DIR / "maps" / "campus.geojson"
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="Les plans du campus n’ont pas encore été importés."
        )
    stat = path.stat()
    return _read_collection(str(path), stat.st_mtime_ns, stat.st_size)


@router.get("/registration")
async def registration(db: AsyncSession = Depends(get_db)):
    from api.private.maps import get_3d_config
    from services.map_registration import registration_from_markers

    from services.scan_ground import scan_ground

    config = await get_3d_config(db=db, current_user=None)
    fitted = registration_from_markers(
        indoor_collection()["features"],
        [marker.model_dump() for marker in config.markers],
    )
    # Markers place the scan horizontally; its height comes from its own ground, so
    # that the scan's ground meets the map's (floors, relief) at 0.
    ground = scan_ground(settings.ASSETS_DIR / "3d") if fitted else None
    if ground is not None:
        fitted = {**fitted, "altitude": ground, "altitude_source": "scan_ground"}
    elif fitted:
        fitted = {**fitted, "altitude_source": "markers"}
    return {"registration": fitted}


@router.get("/manifest")
async def manifest():
    collection = indoor_collection()
    buildings = {}
    for feature in collection["features"]:
        props = feature["properties"]
        bid = props.get("building_id")
        if bid is None:
            continue
        building = buildings.setdefault(
            str(bid), {"id": str(bid), "name": str(bid), "levels": set(), "rooms": 0}
        )
        building["levels"].update(props.get("levels", []))
        if props.get("kind") == "building":
            building["name"] = props.get("name") or building["name"]
            building["feature_id"] = feature["id"]
            import re

            from core.laundry import LAUNDRY_URLS

            residences = sorted(
                set(re.findall(r"\bU[1-7]\b", props.get("name", "").upper()))
            )
            building["residences"] = [
                {"code": code, "laundry": code.lower() in LAUNDRY_URLS}
                for code in residences
            ]
        if props.get("kind") == "room":
            building["rooms"] += 1
    return {
        **collection.get("metadata", {}),
        "buildings": sorted(
            [
                {**building, "levels": sorted(building["levels"])}
                for building in buildings.values()
            ],
            key=lambda building: building["name"],
        ),
    }


@router.get("/geojson")
async def geojson(
    request: Request,
    building: str | None = None,
    level: float | None = None,
    compact: bool = False,
    db: AsyncSession = Depends(get_db),
):
    collection = indoor_collection()
    rows = (
        await db.execute(
            select(
                Location.id,
                Location.attributes["wemap"]["feature_ids"].label("feature_ids"),
                Location.attributes["wemap"]["properties"].label("properties"),
                Location.attributes["wemap"]["active"].label("map_active"),
            ).where(Location.is_active.is_(True))
        )
    ).all()
    links = {}
    overrides = {}
    for location in rows:
        if location.map_active is False:
            continue
        for feature_id in location.feature_ids or []:
            if feature_id in links and links[feature_id] != str(location.id):
                raise HTTPException(
                    status_code=409,
                    detail="Une géométrie est liée à plusieurs lieux. Relancez l’ingestion des plans.",
                )
            links[feature_id] = str(location.id)
            overrides[feature_id] = location.properties or {}
    features = []
    for feature in collection["features"]:
        props = feature["properties"]
        # Curated properties are persisted by ingestion. Geometry always comes
        # from the archived extraction; no identity-specific API exceptions.
        imported = overrides.get(feature["id"], {})
        props = {
            **props,
            **{
                key: imported[key]
                for key in ("name", "building_id", "levels", "category")
                if key in imported
            },
        }
        props["category"] = place_category(props)
        if building is not None and str(props.get("building_id")) != building:
            continue
        if level is not None and props.get("levels") and level not in props["levels"]:
            continue
        if compact:
            # Keep the archival endpoint intact, but avoid transferring duplicated raw
            # provider records and media metadata to the interactive map.
            props = {
                key: value
                for key, value in props.items()
                if key
                in {
                    "source_feature_id",
                    "name",
                    "category",
                    "kind",
                    "levels",
                    "building_id",
                    "label_coordinates",
                    "geometry_area_m2",
                    "description",
                    "tag_names",
                    "address",
                    "access",
                    "opening_hours",
                    "wheelchair",
                    "landuse",
                    "leisure",
                    "category_name",
                    "amenity",
                    "shop",
                    "entrance",
                }
            }
        features.append(
            {
                **feature,
                "properties": {
                    **props,
                    "location_id": links.get(feature["id"]),
                },
            }
        )
    return revalidated(request, {**collection, "features": mark_destinations(features)})


@router.get("/lookup")
async def lookup(
    q: str = Query(min_length=1, max_length=200), db: AsyncSession = Depends(get_db)
):
    """Resolve canonical room codes for links from the timetable and profiles.

    Several records may stand for one place: they are counted once per map
    feature. Between distinct features, the record carrying the exact label
    and then a record attached to a floor win; anything left is ambiguous and
    returned as such. A label matching no name is compared without padding
    zeros ("Amphi 10"), then falls back to its room code ("Salle B313" →
    "B313 Labo INF") when it names one room or one range, whose first mapped
    room stands for the joined rooms.
    """
    normalized = room_alias(q)
    rows = (
        (await db.execute(select(Location).where(Location.is_active.is_(True))))
        .scalars()
        .all()
    )
    mapped = []
    for location in rows:
        geography = (location.attributes or {}).get("wemap", {})
        if geography.get("feature_ids") and geography.get("active", True):
            labels = [
                location.code,
                location.name or "",
                geography.get("properties", {}).get("name", ""),
                *geography.get("aliases", []),
            ]
            mapped.append((location, geography, labels))
    candidates = [
        entry for entry in mapped if normalized in {room_alias(value) for value in entry[2]}
    ]
    if not candidates:
        candidates = [
            entry for entry in mapped if unpadded(q) in {unpadded(value) for value in entry[2]}
        ]
    codes = room_codes(q)
    if not candidates and len(codes) == 1 and "\n" not in q and len(q.split()) <= 6:
        for code in codes[0]:
            candidates = [
                entry
                for entry in mapped
                if any(code in group for value in entry[2] for group in room_codes(value))
            ]
            if candidates:
                break
    by_feature = {}
    for location, geography, labels in candidates:
        feature_id = geography.get("primary_feature_id", geography["feature_ids"][0])
        by_feature.setdefault(feature_id, (location, geography))
    for prefer in (
        lambda location: normalized_name(location.code) == normalized_name(q),
        lambda location: location.parent_id is not None,
    ):
        preferred = {
            feature_id: entry
            for feature_id, entry in by_feature.items()
            if prefer(entry[0])
        }
        if len(by_feature) > 1 and preferred:
            by_feature = preferred
    return [
        {
            "id": str(location.id),
            "code": location.code,
            "name": location.name,
            "feature_id": feature_id,
            "levels": geography.get("levels", []),
            "building_id": geography.get("properties", {}).get("building_id"),
        }
        for feature_id, (location, geography) in by_feature.items()
    ]


@router.get("/legacy")
async def legacy_link(
    building: str | None = Query(None, max_length=40),
    floor: str | None = Query(None, max_length=20),
    room: str | None = Query(None, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    """Campus selection for links to the retired apartments and Foyer pages.

    Their ``bldg``/``floor`` values are historical plan floors, not geographic
    levels: the placements ingested by ``plan_georeference`` translate them.
    """
    from core.housing import housing_plan

    code = building.strip() if building else None
    if code:
        code = "Foyer" if code.lower() == "foyer" else code.upper()
    elif room:
        plan = housing_plan(room.strip())
        code = plan["building"] if plan else ("Foyer" if room.upper().startswith("F") else None)
        floor = floor or (plan and plan["floor"])
    placements = await plan_placements(db)
    target = {"building": None, "level": None, "feature": None}
    local_floors, local_features = local_plans(placements)
    if room:
        wanted = room_alias(room)
        drawn = [
            feature
            for feature in [*projected_plans(placements)[0], *local_features]
            if feature["properties"]["kind"] == "room"
            and (not code or feature["properties"]["plan"]["building"] == code)
            and wanted in {room_alias(feature["properties"]["name"] or ""), room_alias(feature["properties"]["room_id"] or "")}
        ]
        if len(drawn) == 1:
            props = drawn[0]["properties"]
            target = {"building": props["building_id"], "level": (props["levels"] or [None])[0], "feature": drawn[0]["id"]}
        elif not drawn:
            matches = await lookup(room, db)
            if len(matches) == 1:
                match = matches[0]
                target = {"building": match["building_id"], "level": (match["levels"] or [None])[0], "feature": match["feature_id"]}
    if code and target["feature"] is None:
        entry = placements.get(f"{code}_{floor}", {}) if floor else {}
        if entry.get("status") == "placed":
            target["level"] = entry.get("level")
        elif floor and any(item["plan"] == f"{code}_{floor}" for item in local_floors):
            target["level"] = float(floor)
        target["building"] = entry.get("wemap_building_id") or next(
            (
                value["wemap_building_id"]
                for name, value in sorted(placements.items())
                if name.startswith(f"{code}_") and value.get("wemap_building_id") is not None
            ),
            None,
        )
        if target["building"] is None:
            try:
                buildings = (await manifest())["buildings"]
            except HTTPException:
                buildings = []
            target["building"] = next(
                (
                    item["id"]
                    for item in buildings
                    if any(residence["code"] == code for residence in item.get("residences", []))
                    or code.lower() in item["name"].lower().split()
                ),
                None,
            )
        # A residence the map does not know yet still opens on its code.
        target["building"] = target["building"] if target["building"] is not None else code
    if target["building"] is not None:
        target["building"] = str(target["building"])
    return target


async def place_identities(db, location_id):
    """The location and, for a room, the other records of the same canonical room."""
    location = await db.get(Location, location_id)
    if not location or not location.is_active:
        raise HTTPException(status_code=404, detail="Lieu introuvable")
    location_ids = [location.id]
    if location.kind == "ROOM":
        from services.rooms import room_index

        index = await room_index(db)
        canonical = index[1].get(location.id, location.id)
        location_ids = [
            identity for identity, target in index[1].items() if target == canonical
        ] or location_ids
    return location, location_ids


def serialize_event(event):
    return {
        "id": str(event.id),
        "name": event.name,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat(),
    }


@router.get("/locations/{location_id}/events")
async def location_day(location_id: uuid.UUID, day: date = Query(...), db: AsyncSession = Depends(get_db)):
    """One day of the place's agenda (event times are Paris wall-clock, as in ADE)."""
    _, location_ids = await place_identities(db, location_id)
    start = datetime.combine(day, time.min)
    events = (
        (
            await db.execute(
                select(Event)
                .where(
                    Event.location_id.in_(location_ids),
                    Event.is_active.is_(True),
                    Event.end_time > start,
                    Event.start_time < start + timedelta(days=1),
                )
                .order_by(Event.start_time)
            )
        )
        .scalars()
        .all()
    )
    return {"day": day.isoformat(), "events": [serialize_event(event) for event in events]}


@router.get("/locations/{location_id}")
async def location_context(location_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    location, location_ids = await place_identities(db, location_id)
    occupants = (
        (
            await db.execute(
                select(Person)
                .join(PersonHousing, PersonHousing.person_id == Person.id)
                .where(
                    PersonHousing.location_id.in_(location_ids),
                    PersonHousing.ended_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    clubs = (
        (
            await db.execute(
                select(Organization).where(
                    Organization.location_id.in_(location_ids),
                    Organization.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    events = (
        (
            await db.execute(
                select(Event)
                .where(
                    Event.location_id.in_(location_ids),
                    Event.is_active.is_(True),
                    Event.end_time > now,
                    Event.start_time < now + timedelta(days=14),
                )
                .order_by(Event.start_time)
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    has_agenda = (
        await db.execute(
            select(Event.id)
            .where(Event.location_id.in_(location_ids), Event.is_active.is_(True))
            .limit(1)
        )
    ).first() is not None
    return {
        "id": str(location.id),
        "code": location.code,
        "name": location.name,
        "kind": location.kind,
        "attributes": location.attributes,
        "occupants": [
            {
                "id": str(person.id),
                "first_name": person.first_name,
                "last_name": person.last_name,
            }
            for person in occupants
        ],
        "clubs": [{"id": str(club.id), "name": club.name} for club in clubs],
        "events": [serialize_event(event) for event in events],
        # The agenda API exposes room_label (name, falling back to code). Use
        # the same value so a code like A008 can select the label A008 36pl.
        "agenda_room": (location.name or location.code) if has_agenda else None,
        # followme printers standing here, from the seeded print points.
        "printers": (location.attributes or {}).get("printing", {}).get("printers", []),
        "apartment": apartment_details(location.attributes)
        if location.kind == "APARTMENT"
        else None,
    }


@router.get("/housing")
async def housing_summary(db: AsyncSession = Depends(get_db)):
    """Per-residence summary for the building panel: flats per type and rent range."""
    building = aliased(Location)
    rows = (
        await db.execute(
            select(Location.id, Location.attributes, building.code.label("building"))
            .join(building, building.id == Location.parent_id)
            .where(Location.kind == "APARTMENT", Location.is_active.is_(True))
        )
    ).all()
    occupied = set(
        (
            await db.execute(
                select(PersonHousing.location_id).where(
                    PersonHousing.ended_at.is_(None),
                    PersonHousing.location_id.in_([row.id for row in rows]),
                )
            )
        )
        .scalars()
        .all()
    )
    summary = {}
    for row in rows:
        details = apartment_details(row.attributes)
        residence = summary.setdefault(
            row.building, {"apartments": 0, "occupied": 0, "types": {}}
        )
        residence["apartments"] += 1
        residence["occupied"] += row.id in occupied
        kind = residence["types"].setdefault(
            details["type"] or "autre", {"count": 0, "rents": []}
        )
        kind["count"] += 1
        if details["rent"] is not None:
            kind["rents"].append(details["rent"])

    def rent_range(rents):
        return {"min": min(rents), "max": max(rents)} if rents else None

    return {
        code: {
            "apartments": residence["apartments"],
            "occupied": residence["occupied"],
            "rent": rent_range(
                [rent for kind in residence["types"].values() for rent in kind["rents"]]
            ),
            "types": [
                {"type": name, "count": kind["count"], "rent": rent_range(kind["rents"])}
                for name, kind in sorted(
                    residence["types"].items(), key=lambda item: -item[1]["count"]
                )
            ],
        }
        for code, residence in summary.items()
    }


@router.get("/occupancy")
async def occupancy(db: AsyncSession = Depends(get_db)):
    """Occupied and vacant flats, by location, for the map colouring; no identities."""
    apartments = (
        await db.execute(select(Location.id).where(Location.kind == "APARTMENT", Location.is_active.is_(True)))
    ).scalars().all()
    occupied = set(
        (
            await db.execute(
                select(PersonHousing.location_id).where(
                    PersonHousing.ended_at.is_(None),
                    PersonHousing.location_id.in_(apartments),
                )
            )
        ).scalars().all()
    )
    return {
        "occupied": sorted(str(identity) for identity in occupied),
        "vacant": sorted(str(identity) for identity in apartments if identity not in occupied),
    }


def maps_dir():
    return settings.PRIVATE_ASSETS_DIR / "maps"


def map_file(path, media_type, missing):
    if not path.is_file():
        raise HTTPException(status_code=404, detail=missing)
    # Downloaded context changes only when the collection scripts run again.
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/plans")
async def plans(request: Request, db: AsyncSession = Depends(get_db)):
    """Historical drawings placed on the campus, with their authoritative room numbers."""
    placements = await plan_placements(db)
    features, placed = projected_plans(placements)
    unplaced = {name: entry.get("reason") for name, entry in placements.items() if entry.get("status") != "placed"}
    if not features:
        return {"type": "FeatureCollection", "metadata": {"plans": [], "unplaced": unplaced, "superseded": []}, "features": []}
    return revalidated(request, {
        "type": "FeatureCollection",
        "metadata": {
            "plans": list(placed.values()),
            "unplaced": unplaced,
            "computed_at": max((entry.get("computed_at") or "" for entry in placements.values()), default=None),
            "superseded": superseded_places(indoor_collection()["features"], features),
        },
        "features": await link_rooms(db, features),
    })


@router.get("/local-plans")
async def local_plan_view(db: AsyncSession = Depends(get_db)):
    """Residences without any placed floor, drawn in plan coordinates apart from the map."""
    from core.laundry import LAUNDRY_URLS

    floors, features = local_plans(await plan_placements(db))
    buildings = {}
    for floor in floors:
        building = buildings.setdefault(floor["building"], {"code": floor["building"], "laundry": floor["building"].lower() in LAUNDRY_URLS, "floors": []})
        building["floors"].append({key: floor[key] for key in ("plan", "floor", "level", "view_box")})
    return {"buildings": list(buildings.values()), "features": await link_rooms(db, features)}


async def link_rooms(db, features):
    """Flats link to their APARTMENT location, Foyer rooms to their clubs."""
    from api.common import build_foyer_index

    building = aliased(Location)
    apartments = {
        (row.building, row.code): str(row.id)
        for row in (
            await db.execute(
                select(Location.id, Location.code, building.code.label("building"))
                .join(building, building.id == Location.parent_id)
                .where(Location.kind == "APARTMENT", Location.is_active.is_(True))
            )
        ).all()
    }
    foyer, _ = await build_foyer_index(db) if any(feature["properties"]["plan"]["building"] == "Foyer" for feature in features) else ({}, {})
    linked = []
    for feature in features:
        props = feature["properties"]
        if props["kind"] == "room":
            plan = props["plan"]
            extra = {"location_id": apartments.get((plan["building"], props["name"]))}
            room = foyer.get(props["room_id"]) if plan["building"] == "Foyer" else None
            if room:
                extra["clubs"] = [
                    {"id": club["club_id"], "name": club["club_name"], "logo_url": club["logo_url"]}
                    for club in room["clubs"]
                ] or [{"id": None, "name": room["raw_name"], "logo_url": None}]
            feature = {**feature, "properties": {**props, **extra}}
        linked.append(feature)
    return linked


@router.get("/basemap")
async def basemap():
    return map_file(maps_dir() / "basemap.geojson", "application/json", "Le fond de carte n’a pas encore été téléchargé.")


@router.get("/aerial")
async def aerial_index():
    return map_file(maps_dir() / "aerial" / "index.json", "application/json", "Les photographies aériennes n’ont pas encore été téléchargées.")


@router.get("/aerial/{z}/{x}/{y}.jpg")
async def aerial_tile(z: int = Path(ge=0, le=22), x: int = Path(ge=0), y: int = Path(ge=0)):
    return map_file(maps_dir() / "aerial" / str(z) / str(x) / f"{y}.jpg", "image/jpeg", "Tuile absente.")


@router.get("/relief")
async def relief_index():
    return map_file(maps_dir() / "relief" / "index.json", "application/json", "Le relief 3D n’a pas encore été construit.")


@router.get("/relief/{z}/{x}/{y}.bin")
async def relief_tile(z: int = Path(ge=0, le=22), x: int = Path(ge=0), y: int = Path(ge=0)):
    # Height grid and texture of one cell (scripts: ``write_relief_tile``).
    return map_file(maps_dir() / "relief" / str(z) / str(x) / f"{y}.bin", "application/octet-stream", "Tuile absente.")
