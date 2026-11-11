import json
from datetime import datetime, timedelta

import pytest

from api.private import indoor
from db.models import Event, Location, Organization, Person, PersonHousing


@pytest.fixture
def plans(tmp_path, monkeypatch):
    folder = tmp_path / "maps"
    folder.mkdir()
    features = [
        {
            "type": "Feature",
            "id": "building:1",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[2.44, 48.62], [2.45, 48.62], [2.45, 48.63], [2.44, 48.62]]
                ],
            },
            "properties": {
                "source_feature_id": "building:1",
                "kind": "building",
                "name": "U3",
                "building_id": 1,
                "levels": [],
            },
        },
        {
            "type": "Feature",
            "id": "room:1",
            "geometry": {"type": "Point", "coordinates": [2.44, 48.62]},
            "properties": {
                "source_feature_id": "room:1",
                "kind": "room",
                "name": "3050",
                "building_id": 1,
                "levels": [0],
            },
        },
        {
            "type": "Feature",
            "id": "room:2",
            "geometry": {"type": "Point", "coordinates": [2.44, 48.62]},
            "properties": {
                "source_feature_id": "room:2",
                "kind": "room",
                "name": "3150",
                "building_id": 1,
                "levels": [1],
            },
        },
    ]
    (folder / "campus.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "metadata": {"source": "wemap"},
                "features": features,
            }
        )
    )
    monkeypatch.setattr(indoor.settings, "PRIVATE_ASSETS_DIR", tmp_path)
    return features


@pytest.mark.asyncio
async def test_plans_require_auth_and_are_not_exposed_on_public_routes(client, plans):
    assert (await client.get("/api/private/maps/indoor/geojson")).status_code == 401
    assert (await client.get("/api/maps/indoor/geojson")).status_code == 404
    assert (await client.get("/api/assets/maps/campus.geojson")).status_code == 404


@pytest.mark.asyncio
async def test_geojson_floor_filter_and_private_location_links(
    client, db_session, user_token, plans
):
    location = Location(
        kind="APARTMENT",
        code="3050",
        attributes={
            "wemap": {
                "feature_ids": ["room:1"],
                "levels": [0],
                "properties": {"building_id": 1},
            }
        },
    )
    db_session.add(location)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get(
        "/api/private/maps/indoor/geojson?building=1&level=0", headers=headers
    )
    assert response.status_code == 200
    assert {feature["id"] for feature in response.json()["features"]} == {
        "building:1",
        "room:1",
    }
    assert response.json()["features"][1]["properties"]["location_id"] == str(
        location.id
    )
    # The response is a fresh copy: an annotated request must not mutate the cached source.
    assert "location_id" not in indoor.indoor_collection()["features"][1]["properties"]
    manifest = (
        await client.get("/api/private/maps/indoor/manifest", headers=headers)
    ).json()
    assert manifest["buildings"][0]["levels"] == [0, 1]
    matches = (
        await client.get("/api/private/maps/indoor/lookup?q=3050", headers=headers)
    ).json()
    assert matches[0]["feature_id"] == "room:1"


@pytest.mark.asyncio
async def test_occupants_are_returned_only_by_private_context(
    client, db_session, user_token, plans
):
    location = Location(kind="APARTMENT", code="3050")
    person = Person(first_name="Alice", last_name="Martin")
    db_session.add_all([location, person])
    await db_session.flush()
    db_session.add(PersonHousing(person_id=person.id, location_id=location.id))
    await db_session.commit()
    response = await client.get(
        f"/api/private/maps/indoor/locations/{location.id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert response.status_code == 200
    assert response.json()["occupants"][0]["id"] == str(person.id)
    assert (
        await client.get(f"/api/private/maps/indoor/locations/{location.id}")
    ).status_code == 401


@pytest.mark.asyncio
async def test_compact_map_preserves_geometry_and_links_without_raw_provider_records(
    client, db_session, user_token, plans
):
    # A fixture file changes exactly as a new extraction would.
    path = indoor.settings.PRIVATE_ASSETS_DIR / "maps" / "campus.geojson"
    source = json.loads(path.read_text())
    source["features"][1]["properties"]["poi"] = {"raw": "provider metadata"}
    source["features"][1]["properties"]["description"] = "Salle accessible"
    path.write_text(json.dumps(source))
    headers = {"Authorization": f"Bearer {user_token}"}
    full = (
        await client.get("/api/private/maps/indoor/geojson", headers=headers)
    ).json()
    compact = (
        await client.get(
            "/api/private/maps/indoor/geojson?compact=true", headers=headers
        )
    ).json()
    assert [f["geometry"] for f in compact["features"]] == [
        f["geometry"] for f in full["features"]
    ]
    assert [f["id"] for f in compact["features"]] == [f["id"] for f in full["features"]]
    assert "poi" not in compact["features"][1]["properties"]
    assert compact["features"][1]["properties"]["description"] == "Salle accessible"
    assert full["features"][1]["properties"]["poi"] == {"raw": "provider metadata"}
    assert (
        await client.get("/api/private/maps/indoor/geojson?compact=true")
    ).status_code == 401


@pytest.mark.asyncio
async def test_room_actions_include_only_current_memberships_and_upcoming_active_events(
    client, db_session, user_token
):
    now = datetime.now()
    room = Location(kind="ROOM", code="A008", name="A008 36pl")
    current = Person(first_name="Current", last_name="Resident")
    former = Person(first_name="Former", last_name="Resident")
    db_session.add_all([room, current, former])
    await db_session.flush()
    db_session.add_all(
        [
            PersonHousing(location_id=room.id, person_id=current.id),
            PersonHousing(
                location_id=room.id,
                person_id=former.id,
                ended_at=now - timedelta(days=1),
            ),
            Organization(kind="CLUB", name="Active club", location_id=room.id),
            Organization(
                kind="CLUB", name="Archived club", location_id=room.id, is_active=False
            ),
            Event(
                name="Tomorrow",
                location_id=room.id,
                start_time=now + timedelta(days=1),
                end_time=now + timedelta(days=1, hours=1),
            ),
            Event(
                name="Cancelled",
                location_id=room.id,
                start_time=now + timedelta(days=1),
                end_time=now + timedelta(days=1, hours=1),
                is_active=False,
            ),
            Event(
                name="Past",
                location_id=room.id,
                start_time=now - timedelta(days=1, hours=1),
                end_time=now - timedelta(days=1),
            ),
        ]
    )
    await db_session.commit()
    data = (
        await client.get(
            f"/api/private/maps/indoor/locations/{room.id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    ).json()
    assert [person["id"] for person in data["occupants"]] == [str(current.id)]
    assert [club["name"] for club in data["clubs"]] == ["Active club"]
    assert [event["name"] for event in data["events"]] == ["Tomorrow"]
    assert data["agenda_room"] == "A008 36pl"
    rooms = (
        await client.get(
            "/api/private/agenda/rooms/list",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    ).json()
    assert data["agenda_room"] in rooms


@pytest.mark.asyncio
async def test_geojson_uses_persisted_curation_and_aliases_without_identity_specific_code(client, db_session, user_token, plans):
    path = indoor.settings.PRIVATE_ASSETS_DIR / "maps" / "campus.geojson"
    collection = json.loads(path.read_text())
    collection["features"][1]["properties"].pop("name")
    path.write_text(json.dumps(collection))
    location = Location(kind="ROOM", code="Salle A", name="Salle A", attributes={"wemap": {
        "active": True, "feature_ids": ["room:1"], "primary_feature_id": "room:1", "levels": [0],
        "aliases": ["Ancien libellé"], "properties": {"name": "Salle A", "building_id": 1, "levels": [0], "category": "teaching"}}})
    db_session.add(location)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/maps/indoor/geojson?compact=true&building=1&level=0", headers=headers)
    feature = next(feature for feature in response.json()["features"] if feature["id"] == "room:1")
    assert feature["properties"]["name"] == "Salle A"
    assert feature["properties"]["category"] == "teaching"
    assert feature["properties"]["location_id"] == str(location.id)
    assert feature["geometry"] == collection["features"][1]["geometry"]
    assert "name" not in indoor.indoor_collection()["features"][1]["properties"]
    match = (await client.get("/api/private/maps/indoor/lookup?q=Ancien%20libellé", headers=headers)).json()
    assert match[0]["feature_id"] == "room:1"


def mapped_room(code, feature, parent=None, name=None):
    return Location(kind="ROOM", code=code, name=name or code, parent_id=parent, attributes={"wemap": {
        "active": True, "feature_ids": [feature], "primary_feature_id": feature, "levels": [0],
        "properties": {"name": name or code, "building_id": 1}}})


@pytest.mark.asyncio
async def test_lookup_settles_duplicate_records_and_reads_room_codes(client, db_session, user_token):
    floor = Location(kind="FLOOR", code="F0", name="Niveau 0")
    db_session.add(floor)
    await db_session.flush()
    db_session.add_all([
        # A tiered amphitheatre drawn on two levels: the agenda's own record wins.
        mapped_room("AMPHI 010 202pl", "amphi:0", floor.id),
        mapped_room("AMPHI 010", "amphi:-1", floor.id, name="AMPHI 010 202pl"),
        # An outline on a floor and a loose point with the same label: the floor wins.
        mapped_room("AMPHI 011 125pl", "amphi11:outline", floor.id),
        mapped_room("AMPHI 011 125pl", "amphi11:point"),
        mapped_room("B313 Labo INF", "b313", floor.id),
        mapped_room("C405", "c405", floor.id),
        mapped_room("C405 bis", "c405bis", floor.id, name="Salle C405"),
        mapped_room("A004 36pl", "a004", floor.id),
        mapped_room("A001 36pl", "a001", floor.id),
        mapped_room("A002 36pl", "a002", floor.id),
    ])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}

    async def features(q):
        response = await client.get("/api/private/maps/indoor/lookup", params={"q": q}, headers=headers)
        return sorted(match["feature_id"] for match in response.json())

    assert await features("AMPHI 010 202pl") == ["amphi:0"]
    assert await features("AMPHI 011 125pl") == ["amphi11:outline"]
    assert await features("Salle B313") == ["b313"]
    assert await features("Amphi 11") == ["amphi11:outline"]
    # Joined rooms resolve to their first mapped room.
    assert await features("A003-004 36pl") == ["a004"]
    assert await features("A001-002 36pl") == ["a001"]
    # Two distinct rooms sharing a code stay ambiguous.
    assert await features("Amphi C405") == ["c405", "c405bis"]
    assert await features("De A à J : salle B313\nDe K à Z : salle C405") == []
