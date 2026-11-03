from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from core.housing import housing_plan
from core.places import place_category
from db.models import (
    Event,
    Location,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
)
from db.seed import seed_map_catalog, seed_relationship_types
from services.map_registration import fit_registration, registration_from_markers


@pytest.mark.asyncio
async def test_catalog_is_shared_database_metadata_and_public_safe(
    client, db_session, user_token
):
    await seed_map_catalog(db_session)
    await db_session.commit()
    public = (await client.get("/api/maps/catalog")).json()
    assert public["foyer"]["Foyer"][0]["label"] == "Sous-sol (F0)"
    assert (
        public["housing"]["U5"][0]["assets"]["geojson"]
        == "/api/assets/plans/U5_-0.5.geojson"
    )
    floor = (
        (
            await db_session.execute(
                select(Location).where(
                    Location.kind == "FLOOR", Location.code == "-0.5"
                )
            )
        )
        .scalars()
        .one()
    )
    attributes = dict(floor.attributes)
    attributes["legacy_plan"] = {
        **attributes["legacy_plan"],
        "label": "Libellé personnalisé",
        "assets": {"svg": "/api/assets/plans/custom-floor.svg"},
    }
    attributes["notes"] = "Ne doit pas sortir dans le catalogue public"
    floor.attributes = attributes
    await seed_map_catalog(db_session)
    await db_session.commit()
    catalog = (await client.get("/api/maps/catalog")).json()
    assert catalog["housing"]["U5"][0]["label"] == "Libellé personnalisé"
    assert catalog["housing"]["U5"][0]["assets"] == {
        "geojson": "/api/assets/plans/custom-floor.geojson"
    }
    assert "notes" not in str(catalog)
    public_codes = (await client.get("/api/maps/buildings")).json()
    private_codes = (
        await client.get(
            "/api/private/maps/buildings",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    ).json()
    assert public_codes == private_codes
    assert public_codes["U5"] == [row["value"] for row in catalog["housing"]["U5"]]


@pytest.mark.asyncio
async def test_profile_and_graph_share_current_roommates_and_preserve_manual_links(
    client, db_session, user_token
):
    await seed_relationship_types(db_session, log=lambda _: None)
    a, b, old = [
        Person(first_name=name, last_name="Test") for name in ("Alice", "Bob", "Ancien")
    ]
    apartment = Location(kind="APARTMENT", code="5011")
    db_session.add_all([a, b, old, apartment])
    await db_session.flush()
    db_session.add_all(
        [
            PersonHousing(person_id=a.id, location_id=apartment.id),
            PersonHousing(person_id=b.id, location_id=apartment.id),
            PersonHousing(
                person_id=old.id,
                location_id=apartment.id,
                ended_at=datetime.now() - timedelta(days=1),
            ),
        ]
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}
    profile = (
        await client.get(f"/api/private/students/{a.id}/relationships", headers=headers)
    ).json()
    graph = (await client.get("/api/private/graph", headers=headers)).json()
    derived = [edge for edge in graph["links"] if edge.get("origin") == "housing"]
    assert len(derived) == len(profile) == 1
    assert profile[0]["id"] == derived[0]["id"]
    assert profile[0]["derived"] is True
    assert profile[0]["other_student"]["id"] == str(b.id)
    assert (await db_session.execute(select(PersonRelationship))).scalars().all() == []
    relation_type = (
        (
            await db_session.execute(
                select(RelationshipType).where(RelationshipType.name == "Coloc")
            )
        )
        .scalars()
        .one()
    )
    manual = PersonRelationship(
        person_a_id=b.id, person_b_id=a.id, relationship_type_id=relation_type.id
    )
    db_session.add(manual)
    await db_session.commit()
    profile = (
        await client.get(f"/api/private/students/{a.id}/relationships", headers=headers)
    ).json()
    assert len(profile) == 1 and profile[0]["derived"] is False
    graph = (await client.get("/api/private/graph", headers=headers)).json()
    assert len(graph["links"]) == 1
    assert not graph["links"][0].get("derived")
    # Closing housing removes derived links, without changing manual research.
    for assignment in (await db_session.execute(select(PersonHousing))).scalars():
        assignment.ended_at = datetime.now()
    await db_session.commit()
    assert (
        len((await client.get("/api/private/graph", headers=headers)).json()["links"])
        == 1
    )
    assert await db_session.get(PersonRelationship, manual.id) is not None


@pytest.mark.asyncio
async def test_room_aliases_are_resolved_once_on_server_and_reject_ambiguity(
    client, db_session, user_token
):
    room = Location(kind="ROOM", code="A008", name="A008 36pl")
    db_session.add(room)
    await db_session.flush()
    db_session.add(
        Event(
            name="Cours",
            location_id=room.id,
            start_time=datetime(2026, 10, 27, 9),
            end_time=datetime(2026, 10, 27, 10),
        )
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}
    assert (
        await client.get("/api/private/agenda/rooms/resolve?q=a008", headers=headers)
    ).json()["room"] == "A008 36pl"
    assert (
        await client.get("/api/private/agenda/rooms/resolve?q=A00", headers=headers)
    ).json()["room"] is None
    second = Location(kind="ROOM", code="A008 bis", name="A008 20pl")
    db_session.add(second)
    await db_session.flush()
    db_session.add(
        Event(
            name="Autre cours",
            location_id=second.id,
            start_time=datetime(2026, 10, 27, 9),
            end_time=datetime(2026, 10, 27, 10),
        )
    )
    await db_session.commit()
    assert (
        await client.get("/api/private/agenda/rooms/resolve?q=A008", headers=headers)
    ).json() == {"room": None, "ambiguous": True}
    assert (
        await client.get(
            "/api/private/agenda/rooms/resolve?q=A008%2036pl", headers=headers
        )
    ).json()["room"] == "A008 36pl"


def test_housing_plan_and_categories_are_server_normalized():
    assert housing_plan("5009", "U5", "RDC") == {"building": "U5", "floor": "-0.5"}
    assert housing_plan("5010", "U5", "0") == {"building": "U5", "floor": "0.5"}
    assert housing_plan("5010", "U5", "0.5")["floor"] == "0.5"
    assert housing_plan("3150", "U3", "1er")["floor"] == "1"
    assert housing_plan("3050", None, None) == {"building": "U3", "floor": "0"}
    assert housing_plan("Unknown", None, None) is None
    assert place_category({"name": "U1 U2 U4"}) == "housing"
    assert place_category({"name": "Amphithéâtre Étoile"}) == "teaching"
    assert (
        place_category({"name": "Amphithéâtre Étoile", "category": "services"})
        == "services"
    )


def test_registration_recovers_known_transform_and_rejects_ambiguous_buildings():
    import math

    metres = 6378137 * math.pi / 180 * 0.001
    anchors = [
        {"geographic": [0, 0], "scene": [10, 20], "altitude": 5},
        {"geographic": [0.001, 0], "scene": [10, 20 + metres * 2], "altitude": 5},
        {"geographic": [0, 0.001], "scene": [10 + metres * 2, 20], "altitude": 5},
    ]
    result = fit_registration(anchors)
    assert result["a"] == pytest.approx(0)
    assert result["b"] == pytest.approx(2)
    assert result["error"] == pytest.approx(0)
    assert fit_registration([anchors[0]] * 3) is None
    assert fit_registration(anchors[:2]) is None
    feature = {
        "properties": {
            "kind": "building",
            "name": "U1 U2 U4",
            "label_coordinates": [2.44, 48.62],
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[2.44, 48.62], [2.441, 48.62], [2.441, 48.621], [2.44, 48.62]]
            ],
        },
    }

    def marker(code):
        return {
            "bldg_id": code,
            "label": code,
            "footprint": [[0, 0, 0], [10, 0, 0], [10, 0, 10], [0, 0, 10]],
        }

    assert registration_from_markers([feature], [marker("U1"), marker("U2")]) is None
    assert (
        registration_from_markers(
            [feature], [marker("U1"), marker("U1"), marker("U2"), marker("U4")]
        )
        is None
    )


def test_destinations_respect_canonical_location_and_ambiguous_labels():
    from core.places import mark_destinations

    def feature(identity, kind, name, location=None):
        return {
            "id": identity,
            "properties": {
                "name": name,
                "kind": kind,
                "building_id": 1,
                "levels": [0],
                "location_id": location,
            },
        }

    room = feature("r", "room", "Salle A", "a")
    label = feature("p", "poi", "Salle A", "a")
    distinct = feature("q", "poi", "Salle A", "b")
    wall = feature("w", "structure", "Mur")
    anonymous = feature("n", "room", "")
    output = mark_destinations([room, label, distinct, wall, anonymous])
    assert [f["id"] for f in output if f["properties"]["selectable"]] == ["r", "q"]
    ambiguous = mark_destinations(
        [
            feature("r1", "room", "Salle"),
            feature("r2", "room", "Salle"),
            feature("point", "poi", "Salle"),
        ]
    )
    assert all(f["properties"]["selectable"] for f in ambiguous)


@pytest.mark.asyncio
async def test_curated_aliases_share_occupancy_without_rewriting_event_identities(
    client, db_session, user_token
):
    canonical = Location(
        kind="ROOM",
        code="Grande salle 300pl",
        name="Grande salle 300pl",
        attributes={"wemap": {"active": True, "aliases": ["Grande salle"]}},
    )
    legacy = Location(kind="ROOM", code="Grande salle", name="Grande salle")
    db_session.add_all([canonical, legacy])
    await db_session.flush()
    event = Event(
        name="Ancien libellé",
        location_id=legacy.id,
        start_time=datetime(2026, 10, 27, 9),
        end_time=datetime(2026, 10, 27, 10),
    )
    db_session.add(event)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}
    rooms = (await client.get("/api/private/agenda/rooms/list", headers=headers)).json()
    assert rooms == ["Grande salle 300pl"]
    context = (
        await client.get(
            f"/api/private/maps/indoor/locations/{canonical.id}", headers=headers
        )
    ).json()
    assert context["agenda_room"] == "Grande salle 300pl"
    resolved = (
        await client.get(
            "/api/private/agenda/rooms/resolve",
            params={"q": "Grande salle"},
            headers=headers,
        )
    ).json()
    assert resolved == {"room": "Grande salle 300pl", "ambiguous": False}
    occupancy = (
        await client.get(
            "/api/private/agenda/rooms/occupancy",
            params={
                "room_query": "Grande salle 300pl",
                "exact": True,
                "start_date": "2026-10-27",
                "end_date": "2026-10-27",
            },
            headers=headers,
        )
    ).json()
    assert [row["id"] for row in occupancy] == [str(event.id)]
    available = (
        await client.get(
            "/api/private/agenda/rooms/available",
            params={
                "start_time": "2026-10-27T09:15:00",
                "end_time": "2026-10-27T09:45:00",
            },
            headers=headers,
        )
    ).json()
    assert available == []
    assert event.location_id == legacy.id
    assert await db_session.get(Location, legacy.id) is not None
