import json
import math
from datetime import datetime

import pytest

from api.private import indoor
from db.models import Event, Location, Organization, Person, PersonHousing
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
    for path in ("plans", "basemap", "aerial", "aerial/17/1/2.jpg", "relief", "relief/18/1/2.bin", "occupancy"):
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
async def test_drawn_plans_are_revalidated_by_etag(client, db_session, user_token, campus):
    await add_plan_floors(db_session)
    headers = {"Authorization": f"Bearer {user_token}"}
    first = await client.get("/api/private/maps/indoor/plans", headers=headers)
    assert first.headers["cache-control"] == "private, no-cache"
    again = await client.get("/api/private/maps/indoor/plans", headers={**headers, "If-None-Match": first.headers["etag"]})
    assert (again.status_code, again.content) == (304, b"")
    assert (await client.get("/api/private/maps/indoor/plans", headers={**headers, "If-None-Match": '"stale"'})).json() == first.json()


@pytest.mark.asyncio
async def test_occupancy_lists_flats_by_location_without_identities(client, db_session, user_token):
    building = Location(kind="BUILDING", code="U9")
    db_session.add(building)
    await db_session.flush()
    rented, vacant, moved_out = (Location(kind="APARTMENT", code=code, parent_id=building.id) for code in ("9101", "9102", "9103"))
    room = Location(kind="ROOM", code="A008")
    student = Person(kind="STUDENT", first_name="Ada", last_name="L")
    db_session.add_all([rented, vacant, moved_out, room, student])
    await db_session.flush()
    db_session.add_all([
        PersonHousing(person_id=student.id, location_id=rented.id),
        PersonHousing(person_id=student.id, location_id=moved_out.id, ended_at=datetime(2025, 1, 1)),
    ])
    await db_session.commit()

    response = await client.get("/api/private/maps/indoor/occupancy", headers={"Authorization": f"Bearer {user_token}"})
    assert response.json() == {"occupied": [str(rented.id)], "vacant": sorted([str(vacant.id), str(moved_out.id)])}


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
    relief = campus / "relief" / "18" / "1" / "2.bin"
    relief.parent.mkdir(parents=True)
    relief.write_bytes(b"PRH1")

    basemap = await client.get("/api/private/maps/indoor/basemap", headers=headers)
    assert basemap.status_code == 200
    assert basemap.headers["content-type"].startswith("application/json")
    aerial = await client.get("/api/private/maps/indoor/aerial/17/1/2.jpg", headers=headers)
    assert (aerial.status_code, aerial.headers["content-type"], aerial.content) == (200, "image/jpeg", b"\xff\xd8jpeg")
    cell = await client.get("/api/private/maps/indoor/relief/18/1/2.bin", headers=headers)
    assert (cell.status_code, cell.headers["content-type"], cell.content) == (200, "application/octet-stream", b"PRH1")
    assert (await client.get("/api/private/maps/indoor/aerial/17/1/3.jpg", headers=headers)).status_code == 404
    assert (await client.get("/api/private/maps/indoor/aerial/17/..%2F1/2.jpg", headers=headers)).status_code in (404, 422)


MAISEL = {"floor": "Premier étage", "type": "studio", "surface": "22 m²", "price": "550€", "alloc_boursier": "114€", "alloc_non_boursier": "87€", "req_b": 9, "req_e": "1"}


@pytest.mark.asyncio
async def test_apartment_context_exposes_typed_maisel_details(client, db_session, user_token):
    building = Location(kind="BUILDING", code="U9")
    db_session.add(building)
    await db_session.flush()
    apartment = Location(kind="APARTMENT", code="9101", parent_id=building.id, attributes=MAISEL)
    room = Location(kind="ROOM", code="A001", attributes={})
    db_session.add_all([apartment, room])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}

    body = (await client.get(f"/api/private/maps/indoor/locations/{apartment.id}", headers=headers)).json()
    assert body["apartment"] == {
        "type": "studio",
        "floor": "Premier étage",
        "surface_m2": 22,
        "rent": 550,
        "allowances": {"scholarship": 114, "non_scholarship": 87},
        "net_rent": {"scholarship": 436, "non_scholarship": 463},
    }
    assert (await client.get(f"/api/private/maps/indoor/locations/{room.id}", headers=headers)).json()["apartment"] is None


@pytest.mark.asyncio
async def test_housing_summary_counts_types_rents_and_current_occupants(client, db_session, user_token):
    assert (await client.get("/api/private/maps/indoor/housing")).status_code == 401
    building = Location(kind="BUILDING", code="U9")
    db_session.add(building)
    await db_session.flush()
    flats = [
        Location(kind="APARTMENT", code="9101", parent_id=building.id, attributes=MAISEL),
        Location(kind="APARTMENT", code="9102", parent_id=building.id, attributes={**MAISEL, "price": "600€"}),
        Location(kind="APARTMENT", code="9103", parent_id=building.id, attributes={**MAISEL, "type": "chambre", "price": "440€"}),
        Location(kind="APARTMENT", code="9104", parent_id=building.id, attributes=MAISEL, is_active=False),
    ]
    person = Person(kind="STUDENT", first_name="Ada", last_name="L")
    db_session.add_all([*flats, person])
    await db_session.flush()
    db_session.add(PersonHousing(person_id=person.id, location_id=flats[0].id))
    await db_session.commit()

    response = await client.get("/api/private/maps/indoor/housing", headers={"Authorization": f"Bearer {user_token}"})
    assert response.status_code == 200
    assert response.json() == {"U9": {
        "apartments": 3,
        "occupied": 1,
        "rent": {"min": 440, "max": 600},
        "types": [
            {"type": "studio", "count": 2, "rent": {"min": 550, "max": 600}},
            {"type": "chambre", "count": 1, "rent": {"min": 440, "max": 440}},
        ],
    }}


@pytest.mark.asyncio
async def test_drawn_foyer_rooms_list_their_clubs_with_logos(client, db_session, user_token, campus, tmp_path):
    (tmp_path / "public" / "clubs").mkdir()
    (tmp_path / "public" / "clubs" / "foyer_map.csv").write_text("room_id;club_name\nF1-1;DolphINT\nF1-2;Salle libre\n")
    square = {"type": "Polygon", "coordinates": [[[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]]]}
    (tmp_path / "public" / "plans" / "Foyer_1.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "metadata": {"plan_coordinate_space": "local_plan", "building": "Foyer", "floor": "1", "view_box": [0, 0, 100, 100]},
        "features": [
            {"type": "Feature", "id": f"room:{room}", "geometry": None, "properties": {"kind": "room", "building": "Foyer", "floor": "1", "room_id": room, "name": room, "plan_geometry": square, "label_coordinates": [25, 25]}}
            for room in ("F1-1", "F1-2")
        ],
    }))
    foyer = Location(kind="BUILDING", code="Foyer")
    db_session.add(foyer)
    await db_session.flush()
    db_session.add_all([
        Location(kind="FLOOR", code="1", parent_id=foyer.id, attributes={"plan_georeference": {"status": "placed", "building": "Foyer", "floor": "1", "method": "room_shapes", "level": 1.0, "wemap_building_id": 9, "transform": {"a": 0.05, "b": 0.0, "tx": 0.0, "ty": 0.0}, "frame": FRAME}}),
        Organization(kind="CLUB", name="DolphINT", logo_url="/api/assets/clubs/dolphint.png"),
    ])
    await db_session.commit()

    body = (await client.get("/api/private/maps/indoor/plans", headers={"Authorization": f"Bearer {user_token}"})).json()
    clubs = {item["properties"]["room_id"]: item["properties"]["clubs"] for item in body["features"] if item["properties"]["kind"] == "room"}
    assert clubs["F1-1"] == [{"id": clubs["F1-1"][0]["id"], "name": "DolphINT", "logo_url": "/api/assets/clubs/dolphint.png"}]
    assert clubs["F1-2"] == [{"id": None, "name": "Salle libre", "logo_url": None}]


@pytest.mark.asyncio
async def test_legacy_links_resolve_historical_floors_and_rooms_to_the_campus_map(client, db_session, user_token, campus):
    await add_plan_floors(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}

    async def resolve(**params):
        response = await client.get("/api/private/maps/indoor/legacy", params=params, headers=headers)
        assert response.status_code == 200
        return response.json()

    room = {"building": "9", "level": 1.0, "feature": "plan:U9_1:room:9101"}
    assert await resolve(building="u9", floor="1", room="9101") == room
    assert await resolve(room="9101") == room
    assert await resolve(building="U9", floor="1") == {"building": "9", "level": 1.0, "feature": None}
    # An unplaced floor keeps the building, without inventing a geographic level.
    assert await resolve(building="U9", floor="2") == {"building": "9", "level": None, "feature": None}
    # Rooms without a drawn plan fall back to the Wemap lookup.
    assert (await resolve(building="U9", room="Ancien nom"))["feature"] is None
    assert await resolve(building="U8") == {"building": "U8", "level": None, "feature": None}
    assert (await client.get("/api/private/maps/indoor/legacy")).status_code == 401


@pytest.mark.asyncio
async def test_residences_without_placed_floor_are_served_in_plan_coordinates(client, db_session, user_token, campus, tmp_path):
    await add_plan_floors(db_session)
    square = {"type": "Polygon", "coordinates": [[[0, 0], [40, 0], [40, 20], [0, 20], [0, 0]]]}
    (tmp_path / "public" / "plans" / "U8_1.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "metadata": {"plan_coordinate_space": "local_plan", "building": "U8", "floor": "1", "view_box": [0, 0, 200, 900]},
        "features": [
            {"type": "Feature", "id": "plan:U8:1:room:8101", "geometry": None, "properties": {"kind": "room", "building": "U8", "floor": "1", "room_id": "8101", "name": "8101", "plan_geometry": square, "label_coordinates": [20, 10]}},
            {"type": "Feature", "id": "plan:U8:1:space:hall", "geometry": None, "properties": {"kind": "corridor", "building": "U8", "floor": "1", "space_id": "hall", "name": "Couloir", "plan_geometry": square, "label_coordinates": [20, 30]}},
        ],
    }))
    off_campus = Location(kind="BUILDING", code="U8")
    db_session.add(off_campus)
    await db_session.flush()
    flat = Location(kind="APARTMENT", code="8101", parent_id=off_campus.id, attributes=MAISEL)
    db_session.add_all([flat, Location(kind="FLOOR", code="1", parent_id=off_campus.id, attributes={"plan_georeference": {"status": "unplaced", "reason": "Aucun bâtiment géographique correspondant."}})])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}

    assert (await client.get("/api/private/maps/indoor/local-plans")).status_code == 401
    body = (await client.get("/api/private/maps/indoor/local-plans", headers=headers)).json()
    # U9 has a placed floor: it stays on the map, its unplaced floor is not shown apart.
    assert body["buildings"] == [{"code": "U8", "laundry": False, "floors": [{"plan": "U8_1", "floor": "1", "level": 1.0, "view_box": [0, 0, 200, 900]}]}]
    room = body["features"][0]
    assert room["id"] == "plan:U8_1:plan:U8:1:room:8101"
    assert room["geometry"] is None
    assert room["properties"]["plan_geometry"] == square
    assert (room["properties"]["building_id"], room["properties"]["levels"], room["properties"]["location_id"]) == ("U8", [1.0], str(flat.id))
    corridor = body["features"][1]["properties"]
    assert (corridor["kind"], corridor["name"], corridor.get("location_id")) == ("corridor", "Couloir", None)

    resolved = (await client.get("/api/private/maps/indoor/legacy", params={"room": "8101"}, headers=headers)).json()
    assert resolved == {"building": "U8", "level": 1.0, "feature": room["id"]}
    assert (await client.get("/api/private/maps/indoor/legacy", params={"building": "U8", "floor": "1"}, headers=headers)).json() == {"building": "U8", "level": 1.0, "feature": None}


@pytest.mark.asyncio
async def test_place_agenda_returns_one_day_including_events_crossing_midnight(client, db_session, user_token):
    room = Location(kind="ROOM", code="A008", name="A008 36pl")
    other = Location(kind="ROOM", code="B101")
    db_session.add_all([room, other])
    await db_session.flush()
    db_session.add_all([
        Event(name="Cours du matin", location_id=room.id, start_time=datetime(2026, 11, 12, 8, 0), end_time=datetime(2026, 11, 12, 9, 30)),
        Event(name="Soirée", location_id=room.id, start_time=datetime(2026, 11, 11, 22, 0), end_time=datetime(2026, 11, 12, 1, 0)),
        Event(name="Le lendemain", location_id=room.id, start_time=datetime(2026, 11, 13, 8, 0), end_time=datetime(2026, 11, 13, 9, 0)),
        Event(name="Annulé", location_id=room.id, start_time=datetime(2026, 11, 12, 10, 0), end_time=datetime(2026, 11, 12, 11, 0), is_active=False),
        Event(name="Autre salle", location_id=other.id, start_time=datetime(2026, 11, 12, 8, 0), end_time=datetime(2026, 11, 12, 9, 0)),
    ])
    await db_session.commit()
    url = f"/api/private/maps/indoor/locations/{room.id}/events"
    assert (await client.get(url, params={"day": "2026-11-12"})).status_code == 401
    body = (await client.get(url, params={"day": "2026-11-12"}, headers={"Authorization": f"Bearer {user_token}"})).json()
    assert body["day"] == "2026-11-12"
    assert [event["name"] for event in body["events"]] == ["Soirée", "Cours du matin"]
    assert body["events"][1]["start_time"] == "2026-11-12T08:00:00"
    assert (await client.get(url, params={"day": "pas une date"}, headers={"Authorization": f"Bearer {user_token}"})).status_code == 422
