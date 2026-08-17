import asyncio
import os

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_password_hash

from .database import AsyncSessionLocal
from .models import DataSource, Organization, RelationshipType, User

# Registry of every source system that writes into the DB. Loaders reference
# these by `code` (not free text) when stamping provenance on the rows they
# touch, and open an IngestionRun against them for every sync.
DATA_SOURCES = [
    {"code": "trombint", "kind": "SCRAPER", "label": "TrombINT", "description": "Student directory scrape (identity, photos, ecole/promo)."},
    {"code": "agenda_ade", "kind": "SCRAPER", "label": "Agenda ADE", "description": "ADE timetable scrape (courses, exams, rooms)."},
    {"code": "maisel", "kind": "SCRAPER", "label": "MaisEL", "description": "Housing/apartment allocation scrape."},
    {"code": "groupes", "kind": "SCRAPER", "label": "Groupes", "description": "Class-group roster scrape."},
    {"code": "clubs", "kind": "SCRAPER", "label": "Clubs", "description": "Club roster and metadata scrape."},
    {"code": "intllabus", "kind": "SCRAPER", "label": "Catalogue des cours", "description": "Public course catalog scrape (tsp / imt-bs / lsh course sheets)."},
    {"code": "vault_manual", "kind": "MANUAL", "label": "Vault (Manual OSINT)", "description": "Manually researched OSINT data restored from the vault export."},
    {"code": "admin_panel", "kind": "ADMIN", "label": "Admin Panel", "description": "Data entered or edited directly by an admin through the backend."},
]

# Base Organization rows loaders resolve against by (kind, name) instead of
# free-text ecole/promo strings.
SCHOOLS = ["Télécom SudParis", "IMT-BS"]

PROMOS = [
    ("Télécom SudParis", "Ingénieur 1ère année"),
    ("Télécom SudParis", "Ingénieur 2ème année"),
    ("Télécom SudParis", "Ingénieur 3ème année"),
    ("IMT-BS", "Management 1ère année"),
    ("IMT-BS", "Management 2ème année"),
    ("IMT-BS", "Management 3ème année"),
]


async def seed_default_data(db_session: AsyncSession = None, log=print):
    """
    Seeds the database with default metadata (Relationship types, DataSource
    registry, base Organization rows, etc.) and optionally the MCP admin user.
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

        # 3. Seed DataSource registry
        for ds_data in DATA_SOURCES:
            result = await db_session.execute(
                select(DataSource).where(DataSource.code == ds_data["code"])
            )
            existing = result.scalars().first()
            if not existing:
                db_session.add(DataSource(**ds_data))
                log(f"[dim]Seed: Added data source '{ds_data['code']}'[/dim]")

        await db_session.flush()

        # 4. Seed School Organizations
        school_orgs = {}
        for school_name in SCHOOLS:
            result = await db_session.execute(
                select(Organization).where(
                    Organization.kind == "SCHOOL", Organization.name == school_name
                )
            )
            org = result.scalars().first()
            if not org:
                org = Organization(kind="SCHOOL", name=school_name)
                db_session.add(org)
                await db_session.flush()
                log(f"[dim]Seed: Added school '{school_name}'[/dim]")
            school_orgs[school_name] = org

        # 5. Seed Promo Organizations (children of their school)
        for school_name, promo_name in PROMOS:
            result = await db_session.execute(
                select(Organization).where(
                    Organization.kind == "PROMO", Organization.name == promo_name
                )
            )
            existing = result.scalars().first()
            if not existing:
                db_session.add(Organization(
                    kind="PROMO",
                    name=promo_name,
                    parent_id=school_orgs[school_name].id,
                ))
                log(f"[dim]Seed: Added promo '{promo_name}' under '{school_name}'[/dim]")

        # 6. Seed MCP Admin User
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
