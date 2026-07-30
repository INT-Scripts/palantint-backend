import uuid

import pytest
from sqlalchemy.future import select

from conftest import make_student
from db.models import Location, Organization, OrganizationMembership, PersonHousing


@pytest.mark.asyncio
async def test_reassign_housing_closes_old_row_and_opens_new(client, db_session, admin_token, trombint_source):
    student = await make_student(db_session, trombint_source)
    headers = {"Authorization": f"Bearer {admin_token}"}

    res_a = await client.patch(
        f"/api/private/admin/students/{student.id}/apartment",
        json={"apartment": "1101"},
        headers=headers,
    )
    assert res_a.status_code == 200
    assert res_a.json()["apartment"] == "1101"

    res_b = await client.patch(
        f"/api/private/admin/students/{student.id}/apartment",
        json={"apartment": "2202"},
        headers=headers,
    )
    assert res_b.status_code == 200
    assert res_b.json()["apartment"] == "2202"

    result = await db_session.execute(
        select(PersonHousing).where(PersonHousing.person_id == student.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2

    active_rows = [r for r in rows if r.ended_at is None]
    ended_rows = [r for r in rows if r.ended_at is not None]
    assert len(active_rows) == 1
    assert len(ended_rows) == 1

    active_location = await db_session.get(Location, active_rows[0].location_id)
    ended_location = await db_session.get(Location, ended_rows[0].location_id)
    assert active_location.code == "2202"
    assert ended_location.code == "1101"


@pytest.mark.asyncio
async def test_reassign_to_same_apartment_is_a_noop(client, db_session, admin_token, trombint_source):
    student = await make_student(db_session, trombint_source)
    headers = {"Authorization": f"Bearer {admin_token}"}

    await client.patch(
        f"/api/private/admin/students/{student.id}/apartment",
        json={"apartment": "1101"},
        headers=headers,
    )
    res = await client.patch(
        f"/api/private/admin/students/{student.id}/apartment",
        json={"apartment": "1101"},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["apartment"] == "1101"

    result = await db_session.execute(
        select(PersonHousing).where(PersonHousing.person_id == student.id)
    )
    rows = result.scalars().all()
    # Still exactly one row -- reassigning to the same apartment must not
    # close-and-reopen (which would spuriously churn history).
    assert len(rows) == 1
    assert rows[0].ended_at is None


@pytest.mark.asyncio
async def test_assign_ecole_pins_shared_promo_side_effect(client, db_session, admin_token, trombint_source):
    """PINNING TEST -- documents a known, deliberately-not-"fixed" judgment
    call in api/private/admin.py::_assign_ecole (see that function's
    docstring). In the new schema "ecole" is not a per-student field; it's
    derived from the person's active PROMO Organization's parent SCHOOL
    Organization. Editing one student's "ecole" therefore retargets the
    *shared* promo's parent, which silently changes the derived "ecole" for
    every other student in that same promo too. This is intentional,
    surprising, current behavior -- not something this test should "fix".
    If this test starts failing because someone scoped ecole per-student,
    that's a deliberate schema change and this test should be updated
    accordingly, not reverted blindly.
    """
    student_a = await make_student(db_session, trombint_source, "Alice", "Alpha", "aalpha")
    student_b = await make_student(db_session, trombint_source, "Bob", "Beta", "bbeta")

    shared_promo = Organization(id=uuid.uuid4(), kind="PROMO", name="Promo2026")
    db_session.add(shared_promo)
    await db_session.flush()
    db_session.add(OrganizationMembership(person_id=student_a.id, organization_id=shared_promo.id, role="Membre"))
    db_session.add(OrganizationMembership(person_id=student_b.id, organization_id=shared_promo.id, role="Membre"))
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    res = await client.patch(
        f"/api/private/admin/students/{student_a.id}",
        json={"ecole": "EPITA"},
        headers=headers,
    )
    assert res.status_code == 200

    grid_res = await client.get("/api/private/admin/students/grid", headers=headers)
    assert grid_res.status_code == 200
    grid = {row["id"]: row for row in grid_res.json()}

    # BOTH students' derived "ecole" changed, even though only student_a was edited.
    assert grid[str(student_a.id)]["ecole"] == "EPITA"
    assert grid[str(student_b.id)]["ecole"] == "EPITA"
