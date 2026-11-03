from datetime import datetime

import pytest

from db.models import (
    Event,
    EventAttendee,
    EventOrganization,
    Organization,
    OrganizationMembership,
    Person,
)


@pytest.mark.asyncio
async def test_student_agenda_uses_explicit_learners_before_group_fallback(
    client, db_session, user_token
):
    student = Person(kind="STUDENT", first_name="Florian", last_name="Rivière")
    other = Person(kind="STUDENT", first_name="Alice", last_name="Martin")
    group = Organization(kind="CLASS_GROUP", name="Gp-EI2-G")
    db_session.add_all([student, other, group])
    await db_session.flush()
    db_session.add(
        OrganizationMembership(person_id=student.id, organization_id=group.id)
    )

    broad = Event(
        name="Broad group event",
        start_time=datetime(2026, 10, 30, 9),
        end_time=datetime(2026, 10, 30, 10),
    )
    fallback = Event(
        name="Group fallback",
        start_time=datetime(2026, 10, 31, 9),
        end_time=datetime(2026, 10, 31, 10),
    )
    direct = Event(
        name="Direct learner",
        start_time=datetime(2026, 10, 31, 11),
        end_time=datetime(2026, 10, 31, 12),
    )
    archived = Event(
        name="Removed course",
        is_active=False,
        start_time=datetime(2026, 10, 31, 13),
        end_time=datetime(2026, 10, 31, 14),
    )
    db_session.add_all([broad, fallback, direct, archived])
    await db_session.flush()
    db_session.add_all(
        [
            EventOrganization(event_id=broad.id, organization_id=group.id),
            EventOrganization(event_id=fallback.id, organization_id=group.id),
            EventOrganization(event_id=archived.id, organization_id=group.id),
            EventAttendee(event_id=broad.id, person_id=other.id),
            EventAttendee(event_id=direct.id, person_id=student.id),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/private/students/{student.id}/agenda",
        params={"start_date": "2026-10-01", "end_date": "2026-10-31"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert response.status_code == 200
    assert {event["name"] for event in response.json()} == {
        "Group fallback",
        "Direct learner",
    }

    details = await client.get(
        f"/api/private/agenda/events/{broad.id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert details.status_code == 200
    assert {person["id"] for person in details.json()["students"]} == {str(other.id)}

    removed_details = await client.get(
        f"/api/private/agenda/events/{archived.id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert removed_details.status_code == 404


@pytest.mark.asyncio
async def test_comparison_and_room_occupancy_include_events_started_before_range(
    client, db_session, user_token
):
    from db.models import Location

    student = Person(first_name="Night", last_name="Course")
    room = Location(kind="ROOM", code="N1", name="N1")
    db_session.add_all([student, room])
    await db_session.flush()
    event = Event(
        name="Overnight",
        location_id=room.id,
        start_time=datetime(2026, 10, 25, 22),
        end_time=datetime(2026, 10, 26, 10),
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(EventAttendee(event_id=event.id, person_id=student.id))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {user_token}"}
    compared = await client.post(
        "/api/private/agenda/compare",
        headers=headers,
        json={
            "student_ids": [str(student.id)],
            "start_date": "2026-10-26",
            "end_date": "2026-11-01",
        },
    )
    assert compared.status_code == 200
    assert compared.json()[str(student.id)][0]["name"] == "Overnight"
    occupied = await client.get(
        "/api/private/agenda/rooms/occupancy",
        headers=headers,
        params={
            "room_query": "N1",
            "exact": "true",
            "start_date": "2026-10-26",
            "end_date": "2026-11-01",
        },
    )
    assert occupied.status_code == 200
    assert occupied.json()[0]["name"] == "Overnight"


def test_room_queries_do_not_distinct_json_columns_on_postgresql():
    from sqlalchemy.dialects import postgresql
    from api.private.agenda import rooms_with_events

    for statement in [
        rooms_with_events(),
        rooms_with_events(datetime(2026, 10, 26, 9), datetime(2026, 10, 26, 10)),
    ]:
        query = str(statement.compile(dialect=postgresql.dialect()))
        assert "DISTINCT" not in query
        assert "locations.attributes" not in query
        assert "EXISTS" in query
