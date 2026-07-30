import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from core.auth import create_access_token, get_password_hash
from db.database import get_db
from db.models import DataSource, ExternalIdentity, Person
from main import app

# Use an in-memory SQLite for testing. NEVER point tests at the real dev DB.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


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
    from db.models import User

    user = User(
        id=uuid.uuid4(),
        username="testuser",
        hashed_password=get_password_hash("testpassword"),
        is_admin=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_user(db_session):
    from db.models import User

    user = User(
        id=uuid.uuid4(),
        username="adminuser",
        hashed_password=get_password_hash("adminpassword"),
        is_admin=True,
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


@pytest_asyncio.fixture
async def trombint_source(db_session):
    source = DataSource(id=uuid.uuid4(), code="trombint", kind="SCRAPER", label="TrombINT")
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


async def make_student(db_session, trombint_source, first_name="John", last_name="Doe", trombint_id="jdoe"):
    student = Person(id=uuid.uuid4(), kind="STUDENT", first_name=first_name, last_name=last_name)
    db_session.add(student)
    await db_session.flush()
    db_session.add(ExternalIdentity(
        person_id=student.id, source_id=trombint_source.id, external_id=trombint_id,
    ))
    await db_session.commit()
    await db_session.refresh(student)
    return student
