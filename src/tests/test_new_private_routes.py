import uuid

import pytest

from conftest import make_student
from db.models import Location, Organization, OrganizationMembership


@pytest.mark.asyncio
async def test_private_apartment_details_requires_auth(client):
    response = await client.get("/api/private/students/apartments/details")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_private_apartment_details_shape(client, db_session, user_token):
    headers = {"Authorization": f"Bearer {user_token}"}
    building = Location(id=uuid.uuid4(), kind="BUILDING", code="U3", name="U3")
    db_session.add(building)
    await db_session.flush()
    apt = Location(
        id=uuid.uuid4(),
        kind="APARTMENT",
        code="U3-101",
        parent_id=building.id,
        attributes={"floor": "1", "type": "T1", "surface": "18", "price": "400"},
    )
    db_session.add(apt)
    await db_session.commit()

    response = await client.get("/api/private/students/apartments/details", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "U3-101" in data
    assert data["U3-101"]["Bâtiment"] == "U3"
    assert data["U3-101"]["Etage"] == "1"


@pytest.mark.asyncio
async def test_private_maps_buildings_requires_auth(client):
    response = await client.get("/api/private/maps/buildings")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_private_maps_buildings_shape(client, user_token):
    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/maps/buildings", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert len(data) > 0


@pytest.mark.asyncio
async def test_private_class_groups_list_requires_auth(client):
    response = await client.get("/api/private/class-groups")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_private_class_groups_list_shape(client, db_session, user_token, trombint_source):
    headers = {"Authorization": f"Bearer {user_token}"}
    group = Organization(id=uuid.uuid4(), kind="CLASS_GROUP", name="TC-A1")
    db_session.add(group)
    await db_session.flush()

    student = await make_student(db_session, trombint_source)
    db_session.add(
        OrganizationMembership(person_id=student.id, organization_id=group.id, role="Membre")
    )
    await db_session.commit()

    response = await client.get("/api/private/class-groups", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert {"id": str(group.id), "name": "TC-A1"} in data
