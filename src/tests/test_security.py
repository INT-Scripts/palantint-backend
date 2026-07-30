import uuid

import pytest

from db.models import Location, Organization

from conftest import make_student

_make_student = make_student


@pytest.mark.asyncio
async def test_public_search_only_shows_clubs(client, db_session, trombint_source):
    # Setup: Add a student and a club
    student = await _make_student(db_session, trombint_source)
    club = Organization(id=uuid.uuid4(), kind="CLUB", name="Photography Club", slug="photo-club")
    db_session.add(club)
    await db_session.commit()

    # Search without auth (public search)
    response = await client.get("/api/search", params={"q": "John"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["students"]) == 0
    assert len(data["apartments"]) == 0

    response = await client.get("/api/search", params={"q": "Photo"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["clubs"]) > 0
    assert data["clubs"][0]["name"] == "Photography Club"

@pytest.mark.asyncio
async def test_foyer_map_returns_room_mappings(client, db_session):
    # The foyer_map.csv shipped in data/scraps/manual must resolve and be non-empty,
    # and DB clubs must be matched onto their rooms (including multi-club rooms).
    club = Organization(id=uuid.uuid4(), kind="CLUB", name="DolphINT", slug="dolphint")
    db_session.add(club)
    await db_session.commit()

    response = await client.get("/api/foyer/map")
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0

    assert data["F0-4"]["club_id"] == str(club.id)
    assert data["F0-4"]["club_name"] == "DolphINT"

    # F0-2 "Cave (Club Code, ModelIT, GamINT, CELL)" is shared by several clubs
    assert "clubs" in data["F0-2"]


@pytest.mark.asyncio
async def test_private_student_profile_requires_auth(client, db_session, trombint_source):
    student = await _make_student(db_session, trombint_source)

    # Access without auth
    response = await client.get(f"/api/private/students/{student.id}")
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_private_student_profile_accessible_with_auth(client, db_session, user_token, trombint_source):
    student = await _make_student(db_session, trombint_source)

    # Access with auth
    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get(f"/api/private/students/{student.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["first_name"] == "John"

@pytest.mark.asyncio
async def test_admin_routes_require_admin_privileges(client, admin_token, user_token):
    # Try with normal user
    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/admin/telemetry", headers=headers)
    assert response.status_code == 403

    # Try with admin
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.get("/api/private/admin/telemetry", headers=headers)
    assert response.status_code == 200

@pytest.mark.asyncio
async def test_apartment_details_public_access(client, db_session):
    # This route is currently public in api/public/students.py
    building = Location(id=uuid.uuid4(), kind="BUILDING", code="U3", name="U3")
    db_session.add(building)
    await db_session.flush()
    apt = Location(
        id=uuid.uuid4(), kind="APARTMENT", code="U3-101", parent_id=building.id,
        attributes={"floor": "1", "type": "T1", "surface": "18", "price": "400"},
    )
    db_session.add(apt)
    await db_session.commit()

    response = await client.get("/api/students/apartments/details")
    assert response.status_code == 200
    assert "U3-101" in response.json()


@pytest.mark.asyncio
async def test_refresh_token_flow(client, db_session, test_user):
    # Test login returns both tokens
    login_data = {"username": "testuser", "password": "testpassword"}
    response = await client.post("/api/auth/login", data=login_data)
    assert response.status_code == 200
    tokens = response.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    assert tokens["token_type"] == "bearer"

    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # Test refresh token can generate new access token
    refresh_response = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_response.status_code == 200
    new_tokens = refresh_response.json()
    assert "access_token" in new_tokens
    assert "refresh_token" in new_tokens

    # Test using refresh token as access token returns 401
    headers = {"Authorization": f"Bearer {refresh_token}"}
    private_res = await client.get("/api/private/users/me", headers=headers)
    assert private_res.status_code == 401

    # Test using access token to refresh returns 401
    refresh_fail_response = await client.post("/api/auth/refresh", json={"refresh_token": access_token})
    assert refresh_fail_response.status_code == 401
