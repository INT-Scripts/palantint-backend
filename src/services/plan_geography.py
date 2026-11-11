"""Historical floor plans projected onto the geographic campus map.

The scripts derive, per plan, a similarity transform from local drawing units to
metres east/south of a fixed origin and ingest it on the plan's FLOOR location
(``attributes.plan_georeference``). This module applies it to the public plan files and returns private WGS84 features shaped
like the Wemap places (``building_id``, ``levels``, ``kind``, ``label_coordinates``)
so the map can draw both sources with the same layers. The drawings keep their
authoritative room numbers; Wemap rooms they cover are reported as superseded.
"""

import json
import math
from functools import lru_cache

from core.config import settings


def plans_dir():
    return settings.ASSETS_DIR / "plans"


def to_wgs84(frame, transform):
    origin = frame["origin"]
    radius = frame.get("earth_radius", 6378137)
    k = math.pi / 180 * radius
    cos = math.cos(math.radians(origin[1]))
    a, b, tx, ty = transform["a"], transform["b"], transform["tx"], transform["ty"]

    def convert(point):
        east = a * point[0] - b * point[1] + tx
        south = b * point[0] + a * point[1] + ty
        return [round(origin[0] + east / (k * cos), 8), round(origin[1] - south / k, 8)]

    return convert


def map_coordinates(value, convert):
    if value and isinstance(value[0], (int, float)):
        return convert(value)
    return [map_coordinates(item, convert) for item in value]


def point_in_ring(point, ring):
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def polygon_rings(geometry):
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def flatten(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        return [coordinates]
    return [point for item in coordinates for point in flatten(item)]


def bbox(geometry):
    points = flatten(geometry["coordinates"])
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def edge_distance_m(point, ring):
    """Distance in metres from a point to a ring, on a local equirectangular plane."""
    kx = 111320 * math.cos(math.radians(point[1]))
    ky = 110574
    best = math.inf
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        ax, ay = (x1 - point[0]) * kx, (y1 - point[1]) * ky
        bx, by = (x2 - point[0]) * kx, (y2 - point[1]) * ky
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def covers(geometry, point, tolerance_m=1.5):
    """Inside a drawn room, or in the wall gap next to one (the drawings are placed to about a metre)."""
    west, south, east, north = bbox(geometry)
    margin = tolerance_m / 70000
    if not (west - margin <= point[0] <= east + margin and south - margin <= point[1] <= north + margin):
        return False
    rings = [polygon[0] for polygon in polygon_rings(geometry)]
    return any(point_in_ring(point, ring) for ring in rings) or any(edge_distance_m(point, ring) <= tolerance_m for ring in rings)


async def plan_placements(db):
    """Placements ingested on the plan floors, keyed by plan name (``U7_2``)."""
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from db.models import Location

    building = aliased(Location)
    rows = (
        await db.execute(
            select(Location.code, Location.attributes, building.code.label("building"))
            .join(building, building.id == Location.parent_id)
            .where(Location.kind == "FLOOR", Location.is_active.is_(True))
        )
    ).all()
    return {
        f"{row.building}_{row.code}": row.attributes["plan_georeference"]
        for row in rows
        if (row.attributes or {}).get("plan_georeference")
    }


@lru_cache(maxsize=2)
def _projected(placements_key, plans_key):
    placements = json.loads(placements_key)
    features, placed = [], {}
    for name, entry in sorted(placements.items()):
        path = plans_dir() / f"{name}.geojson"
        if entry.get("status") != "placed" or not path.is_file():
            continue
        plan = json.loads(path.read_text(encoding="utf-8"))
        convert = to_wgs84(entry["frame"], entry["transform"]) if "transform" in entry else None
        level = entry.get("level")
        building_id = entry.get("wemap_building_id")
        placed[name] = {
            "plan": name,
            "building": entry["building"],
            "floor": entry["floor"],
            "level": level,
            "building_id": building_id,
            "method": entry["method"],
            "rms_m": entry.get("rms_m", entry.get("mean_distance_m")),
        }
        for feature in plan["features"]:
            props = feature["properties"]
            if props.get("kind") not in ("room", "wall"):
                continue
            geometry = feature.get("geometry")
            if convert:
                geometry = {"type": props["plan_geometry"]["type"], "coordinates": map_coordinates(props["plan_geometry"]["coordinates"], convert)}
            if not geometry:
                continue
            label = props.get("label_coordinates")
            room = props.get("name") or props.get("room_id")
            features.append(
                {
                    "type": "Feature",
                    "id": f"plan:{name}:{feature['id']}",
                    "bbox": list(bbox(geometry)),
                    "geometry": geometry,
                    "properties": {
                        "source": "plan",
                        "kind": props["kind"],
                        "name": room if props["kind"] == "room" else None,
                        "room_id": props.get("room_id"),
                        "building_id": building_id,
                        "levels": [level] if level is not None else [],
                        "label_coordinates": convert(label) if convert and label else label,
                        "plan": {"building": entry["building"], "floor": entry["floor"], "room": room},
                    },
                }
            )
    return features, placed


def projected_plans(placements):
    """(features, placed plans) for the ingested placements; cached per placement set and plan files."""
    plans = plans_dir()
    if not placements or not plans.is_dir():
        return [], {}
    return _projected(json.dumps(placements, sort_keys=True), (str(plans), plans.stat().st_mtime_ns))


def local_plans(placements):
    """Drawings of buildings without any placed floor (U6 is off campus).

    They stay in drawing coordinates, with the same feature identifiers as the
    placed plans, and are shown apart from the geographic map.
    """
    placed = {name.rsplit("_", 1)[0] for name, entry in placements.items() if entry.get("status") == "placed"}
    floors, features = [], []
    for name in sorted(placements):
        building, floor = name.rsplit("_", 1)
        path = plans_dir() / f"{name}.geojson"
        if building in placed or not path.is_file():
            continue
        plan = json.loads(path.read_text(encoding="utf-8"))
        level = float(floor)
        floors.append({"plan": name, "building": building, "floor": floor, "level": level, "view_box": plan["metadata"]["view_box"]})
        for feature in plan["features"]:
            props = feature["properties"]
            if props.get("kind") not in ("room", "wall") or not props.get("plan_geometry"):
                continue
            room = props.get("name") or props.get("room_id")
            features.append({
                "type": "Feature",
                "id": f"plan:{name}:{feature['id']}",
                "geometry": None,
                "properties": {
                    "source": "plan",
                    "local": True,
                    "kind": props["kind"],
                    "name": room if props["kind"] == "room" else None,
                    "room_id": props.get("room_id"),
                    "building_id": building,
                    "levels": [level],
                    "label_coordinates": props.get("label_coordinates"),
                    "plan_geometry": props["plan_geometry"],
                    "plan": {"building": building, "floor": floor, "room": room},
                },
            })
    return floors, features


def superseded_places(wemap_features, plan_features):
    """Wemap rooms and POIs whose label point lies in a drawn room.

    A place on a known level must match the drawing's building and level. A
    pinpoint without a level (shown on every floor) is superseded when it lies
    in a drawn room of any level.
    """
    rooms = {}
    for feature in plan_features:
        props = feature["properties"]
        if props["kind"] == "room" and props["levels"] and feature["geometry"]["type"] in ("Polygon", "MultiPolygon"):
            rooms.setdefault((props["building_id"], props["levels"][0]), []).append(feature["geometry"])
    superseded = []
    for feature in wemap_features:
        props = feature["properties"]
        point = props.get("label_coordinates")
        if props.get("kind") not in ("room", "poi") or not point:
            continue
        levels = props.get("levels") or []
        if levels:
            candidates = [geometry for level in levels for geometry in rooms.get((props.get("building_id"), level), [])]
        else:
            candidates = [geometry for group in rooms.values() for geometry in group]
        if any(covers(geometry, point) for geometry in candidates):
            superseded.append(feature["id"])
    return superseded
