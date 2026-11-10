import json
import math

import pytest

from api.private import indoor
from db.models import Location
from services import plan_geography

ORIGIN = [2.44, 48.62]
K = math.pi / 180 * 6378137


def lonlat(east, south):
    return [
        ORIGIN[0] + east / (K * math.cos(math.radians(ORIGIN[1]))),
        ORIGIN[1] - south / K,
    ]


def feature(identifier, kind, geometry, **properties):
    return {
        "type": "Feature",
        "id": identifier,
        "geometry": geometry,
        "properties": {"source_feature_id": identifier, "kind": kind, **properties},
    }


@pytest.fixture
def campus(tmp_path, monkeypatch):
    maps = tmp_path / "private" / "maps"
    plans = tmp_path / "public" / "plans"
    maps.mkdir(parents=True)
    plans.mkdir(parents=True)
    center = lonlat(2.5, 2.5)
    wemap = [
        feature("building:9", "building", {"type": "Polygon", "coordinates": [[lonlat(-5, -5), lonlat(10, -5), lonlat(10, 10), lonlat(-5, -5)]]}, name="U9", building_id=9, levels=[]),
        feature("room:same-level", "room", {"type": "Point", "coordinates": center}, name="Ancien nom", building_id=9, levels=[1], label_coordinates=center),
        feature("room:other-level", "room", {"type": "Point", "coordinates": center}, name="Autre étage", building_id=9, levels=[2], label_coordinates=center),
        feature("pinpoint:1", "poi", {"type": "Point", "coordinates": center}, name="ST-1 /101", levels=[], label_coordinates=center),
        feature("room:elsewhere", "room", {"type": "Point", "coordinates": lonlat(40, 40)}, name="Loin", building_id=9, levels=[1], label_coordinates=lonlat(40, 40)),
    ]
    (maps / "campus.geojson").write_text(json.dumps({"type": "FeatureCollection", "metadata": {"source": "wemap"}, "features": wemap}))
    square = {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}
    (plans / "U9_1.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "metadata": {"plan_coordinate_space": "local_plan", "building": "U9", "floor": "1", "view_box": [0, 0, 100, 100]},
        "features": [
            {"type": "Feature", "id": "room:9101", "geometry": None, "properties": {"kind": "room", "building": "U9", "floor": "1", "room_id": "9101", "name": "9101", "plan_geometry": square, "label_coordinates": [50, 50]}},
            {"type": "Feature", "id": "wall:1", "geometry": None, "properties": {"kind": "wall", "building": "U9", "floor": "1", "plan_geometry": {"type": "LineString", "coordinates": [[0, 0], [100, 0]]}}},
        ],
    }))
    monkeypatch.setattr(indoor.settings, "PRIVATE_ASSETS_DIR", tmp_path / "private")
    monkeypatch.setattr(indoor.settings, "ASSETS_DIR", tmp_path / "public")
    plan_geography._projected.cache_clear()
    return maps


FRAME = {"origin": ORIGIN, "axes": "east_south_metres", "earth_radius": 6378137}


async def add_plan_floors(db_session):
    """The placements as ``loaders.plan_georeference`` ingests them on the seeded plan floors."""
    building = Location(kind="BUILDING", code="U9", attributes={"legacy_plan": {"category": "housing"}})
    db_session.add(building)
    await db_session.flush()
    db_session.add_all([
        Location(kind="FLOOR", code="1", parent_id=building.id, attributes={
            "legacy_plan": {"label": "1er étage"},
            "plan_georeference": {"status": "placed", "building": "U9", "floor": "1", "method": "room_labels", "anchors": 5, "rms_m": 0.4, "level": 1.0, "wemap_building_id": 9, "transform": {"a": 0.05, "b": 0.0, "tx": 0.0, "ty": 0.0}, "frame": FRAME},
        }),
        Location(kind="FLOOR", code="2", parent_id=building.id, attributes={
            "legacy_plan": {"label": "2e étage"},
            "plan_georeference": {"status": "unplaced", "reason": "Pas assez de numéros de salle communs (0)."},
        }),
    ])
    return building


@pytest.mark.asyncio
async def test_campus_context_routes_are_private(client, campus):
    for path in ("plans", "basemap", "aerial", "aerial/17/1/2.jpg", "relief", "relief/18/1/2.glb"):
        assert (await client.get(f"/api/private/maps/indoor/{path}")).status_code == 401


@pytest.mark.asyncio
async def test_drawn_plans_are_projected_linked_and_supersede_covered_places(client, db_session, user_token, campus):
    building = await add_plan_floors(db_session)
    apartment = Location(kind="APARTMENT", code="9101", parent_id=building.id)
    db_session.add(apartment)
    await db_session.commit()

    response = await client.get("/api/private/maps/indoor/plans", headers={"Authorization": f"Bearer {user_token}"})
    assert response.status_code == 200
    body = response.json()
    room = next(item for item in body["features"] if item["properties"]["kind"] == "room")
    # 100 drawing units at 5 cm per unit: a 5 m square east and south of the origin.
    for actual, expected in zip(room["geometry"]["coordinates"][0][2], lonlat(5, 5)):
        assert actual == pytest.approx(expected, abs=1e-7)
    assert room["properties"]["label_coordinates"] == pytest.approx(lonlat(2.5, 2.5), abs=1e-7)
    assert room["properties"]["levels"] == [1.0]
    assert room["properties"]["building_id"] == 9
    assert room["properties"]["location_id"] == str(apartment.id)
    assert room["properties"]["plan"] == {"building": "U9", "floor": "1", "room": "9101"}
    assert room["bbox"] == pytest.approx([*lonlat(0, 5), *lonlat(5, 0)], abs=1e-7)
    assert any(item["properties"]["kind"] == "wall" for item in body["features"])
    assert set(body["metadata"]["superseded"]) == {"room:same-level", "pinpoint:1"}
    assert body["metadata"]["unplaced"] == {"U9_2": "Pas assez de numéros de salle communs (0)."}


@pytest.mark.asyncio
async def test_plans_are_empty_until_placements_are_ingested(client, user_token, campus):
    response = await client.get("/api/private/maps/indoor/plans", headers={"Authorization": f"Bearer {user_token}"})
    assert response.status_code == 200
    assert response.json()["features"] == []


@pytest.mark.asyncio
async def test_downloaded_context_is_served_only_from_private_map_assets(client, user_token, campus):
    headers = {"Authorization": f"Bearer {user_token}"}
    assert (await client.get("/api/private/maps/indoor/basemap", headers=headers)).status_code == 404
    (campus / "basemap.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    tile = campus / "aerial" / "17" / "1" / "2.jpg"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"\xff\xd8jpeg")
    relief = campus / "relief" / "18" / "1" / "2.glb"
    relief.parent.mkdir(parents=True)
    relief.write_bytes(b"glTF")

    basemap = await client.get("/api/private/maps/indoor/basemap", headers=headers)
    assert basemap.status_code == 200
    assert basemap.headers["content-type"].startswith("application/json")
    aerial = await client.get("/api/private/maps/indoor/aerial/17/1/2.jpg", headers=headers)
    assert (aerial.status_code, aerial.headers["content-type"], aerial.content) == (200, "image/jpeg", b"\xff\xd8jpeg")
    glb = await client.get("/api/private/maps/indoor/relief/18/1/2.glb", headers=headers)
    assert (glb.status_code, glb.headers["content-type"]) == (200, "model/gltf-binary")
    assert (await client.get("/api/private/maps/indoor/aerial/17/1/3.jpg", headers=headers)).status_code == 404
    assert (await client.get("/api/private/maps/indoor/aerial/17/..%2F1/2.jpg", headers=headers)).status_code in (404, 422)
