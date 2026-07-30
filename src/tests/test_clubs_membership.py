import uuid

import pytest
from sqlalchemy.future import select

from conftest import make_student
from db.models import Organization, OrganizationMembership


@pytest.mark.asyncio
async def test_remove_student_club_closes_membership_not_deletes(client, db_session, admin_token, trombint_source):
    student = await make_student(db_session, trombint_source)
    club = Organization(id=uuid.uuid4(), kind="CLUB", name="Robotics Club", slug="robotics")
    db_session.add(club)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    add_res = await client.post(
        f"/api/private/students/{student.id}/clubs",
        json={"club_id": str(club.id), "role": "Membre", "is_mandat": False},
        headers=headers,
    )
    assert add_res.status_code == 200

    remove_res = await client.delete(
        f"/api/private/students/{student.id}/clubs/{club.id}", headers=headers
    )
    assert remove_res.status_code == 200
    assert remove_res.json() == {"status": "removed"}

    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == student.id,
            OrganizationMembership.organization_id == club.id,
        )
    )
    rows = result.scalars().all()
    # Row must still exist (history preserved), just closed out.
    assert len(rows) == 1
    assert rows[0].ended_at is not None


@pytest.mark.asyncio
async def test_re_add_after_removal_current_behavior(client, db_session, admin_token, trombint_source):
    """Documents the CURRENT behavior of re-adding a student to a club they
    were previously removed from.

    NOTE: add_student_club (api/private/clubs.py) only checks for an
    *active* (ended_at IS NULL) membership before deciding whether to
    reject/create. It does not look for a previously-closed row to
    reactivate. So re-adding after a removal creates a brand-new
    OrganizationMembership row rather than reactivating the old one --
    two rows end up existing for the same (student, club) pair, one closed
    and one active. This may or may not be the intended lifecycle (compare
    to _assign_housing/_assign_promo in admin.py, which explicitly
    close-and-reopen without ever leaving more than one row's worth of
    "shape" per active assignment) -- flagging as a scope-ambiguous
    behavior rather than asserting it as clearly desired.
    """
    student = await make_student(db_session, trombint_source)
    club = Organization(id=uuid.uuid4(), kind="CLUB", name="Chess Club", slug="chess")
    db_session.add(club)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(
        f"/api/private/students/{student.id}/clubs",
        json={"club_id": str(club.id), "role": "Membre", "is_mandat": False},
        headers=headers,
    )
    await client.delete(f"/api/private/students/{student.id}/clubs/{club.id}", headers=headers)

    re_add_res = await client.post(
        f"/api/private/students/{student.id}/clubs",
        json={"club_id": str(club.id), "role": "President", "is_mandat": True},
        headers=headers,
    )
    assert re_add_res.status_code == 200

    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == student.id,
            OrganizationMembership.organization_id == club.id,
        )
    )
    rows = result.scalars().all()
    active_rows = [r for r in rows if r.ended_at is None]

    # Pin the actual current behavior: two total rows (one closed, one new
    # active), NOT a single reactivated row.
    assert len(rows) == 2
    assert len(active_rows) == 1
    assert active_rows[0].role == "President"


@pytest.mark.asyncio
async def test_add_student_club_rejects_duplicate_active_membership(client, db_session, admin_token, trombint_source):
    student = await make_student(db_session, trombint_source)
    club = Organization(id=uuid.uuid4(), kind="CLUB", name="Photo Club", slug="photo")
    db_session.add(club)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    payload = {"club_id": str(club.id), "role": "Membre", "is_mandat": False}
    first = await client.post(f"/api/private/students/{student.id}/clubs", json=payload, headers=headers)
    assert first.status_code == 200

    second = await client.post(f"/api/private/students/{student.id}/clubs", json=payload, headers=headers)
    assert second.status_code == 409
