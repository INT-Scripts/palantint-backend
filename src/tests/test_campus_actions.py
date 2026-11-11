from datetime import datetime

import pytest

from db.models import (
    Event,
    EventAttendee,
    EventOrganization,
    Location,
    Organization,
    OrganizationMembership,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
)

NOW = "2026-11-11T10:30:00"


@pytest.fixture
def auth(user_token):
    return {"Authorization": f"Bearer {user_token}"}


async def residence(db_session, *codes):
    building = Location(kind="BUILDING", code="U9")
    db_session.add(building)
    await db_session.flush()
    flats = [Location(kind="APARTMENT", code=code, parent_id=building.id) for code in codes]
    db_session.add_all(flats)
    await db_session.flush()
    return flats


async def people(db_session, *names):
    persons = [Person(kind="STUDENT", first_name=name, last_name="L") for name in names]
    db_session.add_all(persons)
    await db_session.flush()
    return persons


@pytest.mark.asyncio
async def test_campus_action_routes_are_private(client):
    for path in ("people/00000000-0000-0000-0000-000000000000/whereabouts", "locations/00000000-0000-0000-0000-000000000000/residents"):
        assert (await client.get(f"/api/private/maps/indoor/{path}")).status_code == 401


@pytest.mark.asyncio
async def test_whereabouts_gives_the_class_now_and_the_next_one_with_a_room(client, db_session, auth):
    ada, = await people(db_session, "Ada")
    room, other = Location(kind="ROOM", code="A008"), Location(kind="ROOM", code="B112")
    group = Organization(kind="CLASS_GROUP", name="G1")
    db_session.add_all([room, other, group])
    await db_session.flush()
    db_session.add(OrganizationMembership(person_id=ada.id, organization_id=group.id))
    db_session.add_all([
        Event(name="Réseaux", start_time=datetime(2026, 11, 11, 9, 45), end_time=datetime(2026, 11, 11, 11, 15), location_id=room.id, organization_id=group.id),
        Event(name="Sans salle", start_time=datetime(2026, 11, 11, 12, 0), end_time=datetime(2026, 11, 11, 13, 0), organization_id=group.id),
        Event(name="Maths", start_time=datetime(2026, 11, 11, 13, 30), end_time=datetime(2026, 11, 11, 15, 0), location_id=other.id, organization_id=group.id),
        Event(name="Autre groupe", start_time=datetime(2026, 11, 11, 10, 0), end_time=datetime(2026, 11, 11, 11, 0), location_id=other.id),
    ])
    await db_session.commit()

    body = (await client.get(f"/api/private/maps/indoor/people/{ada.id}/whereabouts", params={"at": NOW}, headers=auth)).json()
    assert body["now"]["name"] == "Réseaux" and body["now"]["room"] == "A008"
    assert body["next"]["name"] == "Maths" and body["next"]["room"] == "B112"
    late = (await client.get(f"/api/private/maps/indoor/people/{ada.id}/whereabouts", params={"at": "2026-11-11T20:00:00"}, headers=auth)).json()
    assert late["now"] is None and late["next"] is None


@pytest.mark.asyncio
async def test_residents_list_current_then_past_stays(client, db_session, auth):
    flat, = await residence(db_session, "9101")
    ada, bob, cy = await people(db_session, "Ada", "Bob", "Cy")
    db_session.add_all([
        PersonHousing(person_id=bob.id, location_id=flat.id, started_at=datetime(2023, 9, 1), ended_at=datetime(2024, 7, 1)),
        PersonHousing(person_id=cy.id, location_id=flat.id, started_at=datetime(2024, 9, 1), ended_at=datetime(2025, 7, 1)),
        PersonHousing(person_id=ada.id, location_id=flat.id, started_at=datetime(2025, 9, 1)),
    ])
    await db_session.commit()

    body = (await client.get(f"/api/private/maps/indoor/locations/{flat.id}/residents", headers=auth)).json()
    assert [(item["first_name"], item["ended_at"]) for item in body["residents"]] == [
        ("Ada", None), ("Cy", "2025-07-01T00:00:00"), ("Bob", "2024-07-01T00:00:00"),
    ]


@pytest.mark.asyncio
async def test_circle_places_relations_and_roommates_in_their_flats(client, db_session, auth):
    home, away, empty = await residence(db_session, "9101", "9102", "9103")
    ada, coloc, friend, homeless, moved = await people(db_session, "Ada", "Coloc", "Ami", "Sans", "Parti")
    kind = RelationshipType(name="Ami")
    db_session.add(kind)
    await db_session.flush()
    db_session.add_all([
        PersonHousing(person_id=ada.id, location_id=home.id),
        PersonHousing(person_id=coloc.id, location_id=home.id),
        PersonHousing(person_id=friend.id, location_id=away.id),
        PersonHousing(person_id=moved.id, location_id=empty.id, ended_at=datetime(2025, 1, 1)),
        PersonRelationship(person_a_id=friend.id, person_b_id=ada.id, relationship_type_id=kind.id),
        PersonRelationship(person_a_id=ada.id, person_b_id=homeless.id, relationship_type_id=kind.id),
        PersonRelationship(person_a_id=ada.id, person_b_id=moved.id, relationship_type_id=kind.id),
    ])
    await db_session.commit()

    homes = (await client.get(f"/api/private/maps/indoor/people/{ada.id}/circle", headers=auth)).json()["homes"]
    found = {item["location_id"]: [(person["first_name"], person["link"]) for person in item["people"]] for item in homes}
    assert found == {str(home.id): [("Coloc", "Coloc")], str(away.id): [("Ami", "Ami")]}


@pytest.mark.asyncio
async def test_cohort_prefers_the_class_group_over_the_promo(client, db_session, auth):
    flat_a, flat_b, flat_c = await residence(db_session, "9101", "9102", "9103")
    ada, same_group, same_promo = await people(db_session, "Ada", "Groupe", "Promo")
    promo, group, club = Organization(kind="PROMO", name="2027"), Organization(kind="CLASS_GROUP", name="G1"), Organization(kind="CLUB", name="BDE")
    db_session.add_all([promo, group, club])
    await db_session.flush()
    db_session.add_all([
        *(PersonHousing(person_id=person.id, location_id=flat.id) for person, flat in ((ada, flat_a), (same_group, flat_b), (same_promo, flat_c))),
        *(OrganizationMembership(person_id=person.id, organization_id=promo.id) for person in (ada, same_group, same_promo)),
        *(OrganizationMembership(person_id=person.id, organization_id=group.id) for person in (ada, same_group)),
        *(OrganizationMembership(person_id=person.id, organization_id=club.id) for person in (ada, same_promo)),
    ])
    await db_session.commit()

    body = (await client.get(f"/api/private/maps/indoor/people/{ada.id}/cohort", headers=auth)).json()
    links = {item["people"][0]["first_name"]: item["people"][0]["link"] for item in body["homes"]}
    assert links == {"Groupe": "G1", "Promo": "2027"}
    assert set(body["groups"]) == {"G1", "2027"}


@pytest.mark.asyncio
async def test_organization_homes_flag_the_board(client, db_session, auth):
    flat_a, flat_b = await residence(db_session, "9101", "9102")
    president, member = await people(db_session, "Présidente", "Membre")
    club = Organization(kind="CLUB", name="BDE")
    db_session.add(club)
    await db_session.flush()
    board = Organization(kind="BUREAU", name="Bureau BDE", parent_id=club.id)
    db_session.add(board)
    await db_session.flush()
    db_session.add_all([
        PersonHousing(person_id=president.id, location_id=flat_a.id),
        PersonHousing(person_id=member.id, location_id=flat_b.id),
        OrganizationMembership(person_id=president.id, organization_id=club.id),
        OrganizationMembership(person_id=president.id, organization_id=board.id, role="Présidente"),
        OrganizationMembership(person_id=member.id, organization_id=club.id),
    ])
    await db_session.commit()

    body = (await client.get(f"/api/private/maps/indoor/organizations/{club.id}/homes", headers=auth)).json()
    assert (body["board"], body["members"]) == (1, 2)
    people_by_name = {item["people"][0]["first_name"]: item["people"][0] for item in body["homes"]}
    assert people_by_name["Présidente"]["board"] is True and people_by_name["Présidente"]["link"] == "Présidente"
    assert people_by_name["Membre"]["board"] is False and people_by_name["Membre"]["link"] == "Membre"


@pytest.mark.asyncio
async def test_presence_uses_attendees_or_else_the_class_groups(client, db_session, auth):
    flat_a, flat_b = await residence(db_session, "9101", "9102")
    ada, bob = await people(db_session, "Ada", "Bob")
    room = Location(kind="ROOM", code="A008")
    group = Organization(kind="CLASS_GROUP", name="G1")
    db_session.add_all([room, group])
    await db_session.flush()
    course = Event(name="Réseaux", start_time=datetime(2026, 11, 11, 9, 45), end_time=datetime(2026, 11, 11, 11, 15), location_id=room.id)
    seminar = Event(name="Séminaire", start_time=datetime(2026, 11, 11, 14, 0), end_time=datetime(2026, 11, 11, 15, 0), location_id=room.id)
    db_session.add_all([course, seminar])
    await db_session.flush()
    db_session.add_all([
        EventOrganization(event_id=course.id, organization_id=group.id),
        EventAttendee(event_id=seminar.id, person_id=bob.id),
        PersonHousing(person_id=ada.id, location_id=flat_a.id),
        PersonHousing(person_id=bob.id, location_id=flat_b.id),
        OrganizationMembership(person_id=ada.id, organization_id=group.id),
    ])
    await db_session.commit()

    url = f"/api/private/maps/indoor/locations/{room.id}/presence"
    now = (await client.get(url, params={"at": NOW}, headers=auth)).json()
    assert (now["event"]["name"], now["ongoing"], now["people"]) == ("Réseaux", True, 1)
    assert now["groups"] == [{"id": str(group.id), "name": "G1"}]
    assert now["homes"] == [{"location_id": str(flat_a.id), "people": [{"id": str(ada.id), "first_name": "Ada", "last_name": "L", "link": "G1"}]}]
    # Between classes, the last one of the day; listed attendees win over groups.
    after = (await client.get(url, params={"at": "2026-11-11T16:00:00"}, headers=auth)).json()
    assert (after["event"]["name"], after["ongoing"], after["homes"][0]["people"][0]["first_name"]) == ("Séminaire", False, "Bob")
    before = (await client.get(url, params={"at": "2026-11-11T08:00:00"}, headers=auth)).json()
    assert before["event"] is None


@pytest.mark.asyncio
async def test_week_groups_count_sessions_of_the_week_only(client, db_session, auth):
    room = Location(kind="ROOM", code="A008")
    g1, g2 = Organization(kind="CLASS_GROUP", name="G1"), Organization(kind="CLASS_GROUP", name="G2")
    db_session.add_all([room, g1, g2])
    await db_session.flush()
    # Wednesday 11 November 2026: the week runs from Monday 9 to Sunday 15.
    db_session.add_all([
        Event(name="A", start_time=datetime(2026, 11, 9, 8, 0), end_time=datetime(2026, 11, 9, 9, 0), location_id=room.id, organization_id=g1.id),
        Event(name="B", start_time=datetime(2026, 11, 13, 8, 0), end_time=datetime(2026, 11, 13, 9, 0), location_id=room.id, organization_id=g1.id),
        Event(name="C", start_time=datetime(2026, 11, 15, 8, 0), end_time=datetime(2026, 11, 15, 9, 0), location_id=room.id, organization_id=g2.id),
        Event(name="Next week", start_time=datetime(2026, 11, 16, 8, 0), end_time=datetime(2026, 11, 16, 9, 0), location_id=room.id, organization_id=g2.id),
    ])
    await db_session.commit()

    body = (await client.get(f"/api/private/maps/indoor/locations/{room.id}/groups", params={"day": "2026-11-11"}, headers=auth)).json()
    assert body["week"] == "2026-11-09" and body["events"] == 3
    assert [(group["name"], group["sessions"]) for group in body["groups"]] == [("G1", 2), ("G2", 1)]
