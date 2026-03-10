import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

load_dotenv()

# Use 127.0.0.1 instead of localhost to avoid IPv6 resolution issues (Errno 111)
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://postgres:password123@127.0.0.1:5432/palantint"
)

# Parse DATABASE_URL to get the base connection (to default postgres DB)
from sqlalchemy import make_url

url = make_url(DATABASE_URL)
POSTGRES_DB_URL = url.set(database="postgres").render_as_string(hide_password=False)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session



async def init_db():
    # We must import models inside here to ensure they are registered with SQLModel.metadata
    from . import models

    import asyncio
    import asyncpg
    from sqlalchemy.exc import DBAPIError

    # Parse DATABASE_URL
    from sqlalchemy import make_url
    url = make_url(DATABASE_URL)
    
    db_name = url.database
    db_user = url.username
    db_pass = url.password
    db_host = url.host
    db_port = url.port or 5432

    max_retries = 30
    retry_delay = 1

    print(f"📡 Initializing database connection to {db_host}:{db_port}...")

    for i in range(max_retries):
        try:
            # 1. Try to connect to 'postgres' to ensure server is up and create DB if needed
            conn = await asyncpg.connect(
                user=db_user,
                password=db_pass,
                host=db_host,
                port=db_port,
                database="postgres"
            )
            try:
                # Check if target DB exists
                db_exists = await conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", db_name
                )
                if not db_exists:
                    print(f"⚠️  Database '{db_name}' does not exist. Creating it...")
                    await conn.execute(f'CREATE DATABASE "{db_name}"')
                    print(f"✅ Database '{db_name}' created.")
            finally:
                await conn.close()

            # 2. Now initialize SQLModel schema using SQLAlchemy engine
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            
            # 3. Seed default metadata
            from .seed import seed_default_data
            await seed_default_data()
            
            print("✅ Database schema and default data initialized successfully.")
            return

        except Exception as e:
            if i < max_retries - 1:
                print(f"⏳ Database initialization failed (Attempt {i+1}/{max_retries}). Error: {str(e)[:100]}")
                await asyncio.sleep(retry_delay)
            else:
                print(f"❌ Database initialization failed after {max_retries} attempts.")
                raise e


if __name__ == "__main__":
    import asyncio
    asyncio.run(init_db())
