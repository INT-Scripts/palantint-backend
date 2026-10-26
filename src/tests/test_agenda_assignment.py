from datetime import datetime

import pytest

from db.models import Event, EventAttendee, EventOrganization, Organization, OrganizationMembership, Person


@pytest.mark.asyncio
async def test_student_agenda_uses_explicit_learners_before_group_fallback(client, db_session, user_token):
    student = Person(kind="STUDENT", first_name="Florian", last_name="Rivière")
    other = Person(kind="STUDENT", first_name="Alice", last_name="Martin")
    group = Organization(kind="CLASS_GROUP", name="Gp-EI2-G")
    db_session.add_all([student, other, group])
    await db_session.flush()
    db_session.add(OrganizationMembership(person_id=student.id, organization_id=group.id))

    broad = Event(name="Broad group event", start_time=datetime(2026, 10, 30, 9), end_time=datetime(2026, 10, 30, 10))
    fallback = Event(name="Group fallback", start_time=datetime(2026, 10, 31, 9), end_time=datetime(2026, 10, 31, 10))
    direct = Event(name="Direct learner", start_time=datetime(2026, 10, 31, 11), end_time=datetime(2026, 10, 31, 12))
    archived = Event(name="Removed course", is_active=False, start_time=datetime(2026, 10, 31, 13), end_time=datetime(2026, 10, 31, 14))
    db_session.add_all([broad, fallback, direct, archived])
    await db_session.flush()
    db_session.add_all([
        EventOrganization(event_id=broad.id, organization_id=group.id),
        EventOrganization(event_id=fallback.id, organization_id=group.id),
        EventOrganization(event_id=archived.id, organization_id=group.id),
        EventAttendee(event_id=broad.id, person_id=other.id),
        EventAttendee(event_id=direct.id, person_id=student.id),
    ])
    await db_session.commit()

    response = await client.get(
        f"/api/private/students/{student.id}/agenda",
        params={"start_date": "2026-10-01", "end_date": "2026-10-31"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert response.status_code == 200
    assert {event["name"] for event in response.json()} == {"Group fallback", "Direct learner"}

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
