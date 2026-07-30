import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from core.auth import create_access_token, get_password_hash
from db.database import get_db
from db.models import ApartmentDetail, Club, Student, User
from main import app

# Use an in-memory SQLite for testing if possible, or a dedicated test DB.
# For now, let's assume a test database URL or use the environment one.
# IMPORTANT: In a real scenario, we should NEVER use the production DB for tests.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

from sqlalchemy import event


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    
    @event.listens_for(engine.sync_engine, "connect")
    def receive_connect(dbapi_connection, connection_record):
        dbapi_connection.create_function("unaccent", 1, lambda x: x if x else x)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()

@pytest_asyncio.fixture
async def db_session(test_engine):
    SessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
        # Clean up data after each test
        for table in reversed(SQLModel.metadata.sorted_tables):
            await session.execute(table.delete())
        await session.commit()

@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session
    
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()

@pytest_asyncio.fixture
async def test_user(db_session):
    user = User(
        id=uuid.uuid4(),
        username="testuser",
        hashed_password=get_password_hash("testpassword"),
        is_admin=False
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user

@pytest_asyncio.fixture
async def admin_user(db_session):
    user = User(
        id=uuid.uuid4(),
        username="adminuser",
        hashed_password=get_password_hash("adminpassword"),
        is_admin=True
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user

@pytest_asyncio.fixture
def user_token(test_user):
    return create_access_token(data={"sub": test_user.username, "is_admin": False})

@pytest_asyncio.fixture
def admin_token(admin_user):
    return create_access_token(data={"sub": admin_user.username, "is_admin": True})

@pytest.mark.asyncio
async def test_public_search_only_shows_clubs(client, db_session):
    # Setup: Add a student and a club
    student = Student(id=uuid.uuid4(), first_name="John", last_name="Doe", trombint_id="jdoe")
    club = Club(id=uuid.uuid4(), name="Photography Club", slug="photo-club")
    db_session.add_all([student, club])
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
    club = Club(id=uuid.uuid4(), name="DolphINT", slug="dolphint")
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
async def test_private_student_profile_requires_auth(client, db_session):
    student_id = uuid.uuid4()
    student = Student(id=student_id, first_name="John", last_name="Doe", trombint_id="jdoe")
    db_session.add(student)
    await db_session.commit()

    # Access without auth
    response = await client.get(f"/api/private/students/{student_id}")
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_private_student_profile_accessible_with_auth(client, db_session, user_token):
    student_id = uuid.uuid4()
    student = Student(id=student_id, first_name="John", last_name="Doe", trombint_id="jdoe")
    db_session.add(student)
    await db_session.commit()

    # Access with auth
    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get(f"/api/private/students/{student_id}", headers=headers)
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
    # This route is currently public in api_students.py
    apt = ApartmentDetail(id="U3-101", building="U3", floor="1", type="T1", surface="18", price="400")
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
