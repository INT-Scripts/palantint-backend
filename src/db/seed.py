import asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from .database import AsyncSessionLocal
from .models import RelationshipType

async def seed_default_data(db_session: AsyncSession = None, log=print):
    """
    Seeds the database with default metadata (Relationship types, etc.)
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

        await db_session.flush()
        
        if local_session:
            await db_session.commit()
            
    finally:
        if local_session:
            await db_session.close()

if __name__ == "__main__":
    asyncio.run(seed_default_data())
