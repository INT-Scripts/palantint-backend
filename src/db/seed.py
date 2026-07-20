import asyncio
import os

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_password_hash

from .database import AsyncSessionLocal
from .models import RelationshipType, User


async def seed_default_data(db_session: AsyncSession = None, log=print):
    """
    Seeds the database with default metadata (Relationship types, etc.)
    and optionally the MCP admin user.
    """
    local_session = False
    if db_session is None:
        db_session = AsyncSessionLocal()
        local_session = True

    try:
        # 1. Enable unaccent extension
        try:
            await db_session.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent;"))
            log("[dim]Database: Extension 'unaccent' ensured.[/dim]")
        except Exception as e:
            log(f"[yellow]Warning: Could not create unaccent extension: {e}[/yellow]")

        # 2. Seed Relationship Types
        defaults = [
            {"name": "Amis", "color": "#3b82f6"},
            {"name": "En couple", "color": "#ec4899"},
            {"name": "Ex", "color": "#ef4444"},
        ]

        for rt_data in defaults:
            result = await db_session.execute(
                select(RelationshipType).where(RelationshipType.name == rt_data["name"])
            )
            existing = result.scalars().first()
            if not existing:
                db_session.add(RelationshipType(**rt_data))
                log(f"[dim]Seed: Added relationship type '{rt_data['name']}'[/dim]")

        # 3. Seed MCP Admin User
        mcp_user = os.environ.get("MCP_PALANTINT_USERNAME")
        mcp_pass = os.environ.get("MCP_PALANTINT_PASSWORD")

        if mcp_user and mcp_pass:
            result = await db_session.execute(
                select(User).where(User.username == mcp_user)
            )
            existing_user = result.scalars().first()
            if not existing_user:
                db_session.add(User(
                    username=mcp_user,
                    hashed_password=get_password_hash(mcp_pass),
                    is_admin=True
                ))
                log(f"[dim]Seed: Created MCP admin user '{mcp_user}'[/dim]")
            else:
                # Optional: Ensure it has admin rights if it exists
                if not existing_user.is_admin:
                    existing_user.is_admin = True
                    log(f"[dim]Seed: Upgraded existing user '{mcp_user}' to admin[/dim]")

        await db_session.flush()
        
        if local_session:
            await db_session.commit()
            
    finally:
        if local_session:
            await db_session.close()

if __name__ == "__main__":
    asyncio.run(seed_default_data())
