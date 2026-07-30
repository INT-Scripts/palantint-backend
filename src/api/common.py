"""Shared query helpers for the Person/Organization/Location model.

API responses keep emitting the old flat fields (`promo`, `ecole`,
`apartment`, `trombint_id`) for frontend stability, translating from the new
relational shape at the boundary — see AGENTS.md "Modele de Donnees" for the
rationale. New richer shapes (e.g. `housing`, `memberships`) are added
alongside rather than replacing them.
"""
import csv
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.config import settings
from db.models import DataSource, ExternalIdentity, Location, Organization, OrganizationMembership, PersonHousing

TROMBINT_SOURCE_CODE = "trombint"
CLUB_KINDS = ("CLUB", "BUREAU")


async def get_data_source_id(db: AsyncSession, code: str) -> Optional[uuid.UUID]:
    result = await db.execute(select(DataSource.id).where(DataSource.code == code))
    return result.scalar_one_or_none()


def promo_name_subquery(person_id_col):
    PromoOrg = aliased(Organization)
    return (
        select(PromoOrg.name)
        .select_from(OrganizationMembership)
        .join(PromoOrg, PromoOrg.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.person_id == person_id_col,
            OrganizationMembership.ended_at.is_(None),
            PromoOrg.kind == "PROMO",
        )
        .limit(1)
        .correlate_except(PromoOrg)
        .scalar_subquery()
    )


def school_name_subquery(person_id_col):
    SchoolOrg = aliased(Organization)
    PromoOrg = aliased(Organization)
    return (
        select(SchoolOrg.name)
        .select_from(OrganizationMembership)
        .join(PromoOrg, PromoOrg.id == OrganizationMembership.organization_id)
        .join(SchoolOrg, SchoolOrg.id == PromoOrg.parent_id)
        .where(
            OrganizationMembership.person_id == person_id_col,
            OrganizationMembership.ended_at.is_(None),
            PromoOrg.kind == "PROMO",
            SchoolOrg.kind == "SCHOOL",
        )
        .limit(1)
        .correlate_except(SchoolOrg, PromoOrg)
        .scalar_subquery()
    )


def trombint_id_subquery(person_id_col):
    return (
        select(ExternalIdentity.external_id)
        .select_from(ExternalIdentity)
        .join(DataSource, DataSource.id == ExternalIdentity.source_id)
        .where(
            ExternalIdentity.person_id == person_id_col,
            DataSource.code == TROMBINT_SOURCE_CODE,
        )
        .limit(1)
        .correlate_except(ExternalIdentity, DataSource)
        .scalar_subquery()
    )


def apartment_code_subquery(person_id_col):
    return (
        select(Location.code)
        .select_from(PersonHousing)
        .join(Location, Location.id == PersonHousing.location_id)
        .where(
            PersonHousing.person_id == person_id_col,
            PersonHousing.ended_at.is_(None),
        )
        .limit(1)
        .correlate_except(Location, PersonHousing)
        .scalar_subquery()
    )


async def get_trombint_id(db: AsyncSession, person_id: uuid.UUID) -> Optional[str]:
    result = await db.execute(
        select(ExternalIdentity.external_id)
        .join(DataSource, DataSource.id == ExternalIdentity.source_id)
        .where(ExternalIdentity.person_id == person_id, DataSource.code == TROMBINT_SOURCE_CODE)
    )
    return result.scalar_one_or_none()


async def get_active_promo_school(db: AsyncSession, person_id: uuid.UUID) -> tuple[Optional[str], Optional[str]]:
    PromoOrg = aliased(Organization)
    SchoolOrg = aliased(Organization)
    result = await db.execute(
        select(PromoOrg.name, SchoolOrg.name)
        .select_from(OrganizationMembership)
        .join(PromoOrg, PromoOrg.id == OrganizationMembership.organization_id)
        .outerjoin(SchoolOrg, SchoolOrg.id == PromoOrg.parent_id)
        .where(
            OrganizationMembership.person_id == person_id,
            OrganizationMembership.ended_at.is_(None),
            PromoOrg.kind == "PROMO",
        )
        .limit(1)
    )
    row = result.first()
    return (row[0], row[1]) if row else (None, None)


async def get_active_housing(db: AsyncSession, person_id: uuid.UUID) -> Optional[Location]:
    result = await db.execute(
        select(Location)
        .join(PersonHousing, PersonHousing.location_id == Location.id)
        .where(PersonHousing.person_id == person_id, PersonHousing.ended_at.is_(None))
    )
    return result.scalars().first()


# ── Clubs / Foyer (shared between the public and private routers) ──────────
# Organization.kind is the source of truth for what counts as a club-facing
# entity (CLUB/BUREAU — see scripts/loaders/clubs.py:_kind_for). ADMIN-kind
# rows (school administration, deleted-org placeholders) are deliberately
# excluded everywhere CLUB_KINDS is used.

def club_type(club: Organization) -> str:
    return "Association" if club.kind == "BUREAU" else "Club"


def serialize_club_summary(club: Organization) -> Dict[str, Any]:
    return {
        "club_id": str(club.id),
        "club_name": club.name,
        "logo_url": club.logo_url,
        "description": club.description,
        "type": club_type(club),
        "association_of_origin": club.attributes.get("association_of_origin"),
    }


def _extract_candidate_names(raw_name: str) -> List[str]:
    """
    Some foyer rooms are shared by several clubs, encoded either as a
    parenthetical list ("Cave (Club Code, ModelIT, GamINT, CELL)") or as a
    bare comma-separated list ("PaintIT,TellTheTale,INTimes"). Others use
    the parens for a location detail on a single club ("Minet (Bagagerie)").
    Returns the list of candidate club names to try matching against the DB.
    """
    raw_name = raw_name.strip()
    if not raw_name:
        return []

    match = re.match(r"^(.*?)\((.*)\)\s*$", raw_name)
    if match:
        base, inner = match.group(1).strip(), match.group(2).strip()
        if "," in inner:
            return [t.strip() for t in inner.split(",") if t.strip()]
        return [base] if base else [inner]

    if "," in raw_name:
        return [t.strip() for t in raw_name.split(",") if t.strip()]

    return [raw_name]


async def build_foyer_index(db: AsyncSession) -> Tuple[Dict[str, Any], Dict[uuid.UUID, str]]:
    """
    Single source of truth for foyer room <-> club resolution, shared by
    GET /clubs, GET /foyer/map and GET /clubs/{id} on both routers. Reads
    the CSV export of data/scraps/manual/foyer_map.csv (see
    scripts/src/palantint_scripts/map_gen.py) and name-matches each room's
    occupant(s) against Organization — there is no FK for this relationship.

    Returns (rooms_by_id, room_id_by_club_id).
    """
    csv_path = os.path.join(str(settings.ASSETS_DIR / "clubs"), "foyer_map.csv")
    if not os.path.exists(csv_path):
        return {}, {}

    entries = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            room_id = row.get("room_id", "").strip()
            club_name = row.get("club_name", "").strip()
            if room_id:
                floor = "0" if room_id.startswith("F0") else ("1" if room_id.startswith("F1") else "0")
                entries.append({"room_id": room_id, "raw_name": club_name, "floor": floor})

    res = await db.execute(select(Organization).where(Organization.kind.in_(CLUB_KINDS)))
    clubs_list = res.scalars().all()
    clubs_by_name = {c.name.lower(): c for c in clubs_list}

    rooms_by_id: Dict[str, Any] = {}
    room_id_by_club_id: Dict[uuid.UUID, str] = {}

    for entry in entries:
        room_id, raw_name, floor = entry["room_id"], entry["raw_name"], entry["floor"]
        matched_clubs: List[Organization] = []

        for candidate in _extract_candidate_names(raw_name):
            candidate_lower = candidate.lower()
            club_obj: Optional[Organization] = clubs_by_name.get(candidate_lower)
            if not club_obj:
                for db_name_lower, c in clubs_by_name.items():
                    if candidate_lower and (candidate_lower in db_name_lower or db_name_lower in candidate_lower):
                        club_obj = c
                        break
            if club_obj and club_obj.id not in {c.id for c in matched_clubs}:
                matched_clubs.append(club_obj)

        primary = matched_clubs[0] if matched_clubs else None
        rooms_by_id[room_id] = {
            "room_id": room_id,
            "raw_name": raw_name,
            "club_name": primary.name if primary else raw_name,
            "club_id": str(primary.id) if primary else None,
            "logo_url": primary.logo_url if primary else None,
            "description": primary.description if primary else None,
            "type": club_type(primary) if primary else "Club",
            "association_of_origin": primary.attributes.get("association_of_origin") if primary else None,
            "floor": floor,
            "building": "Foyer",
            "clubs": [serialize_club_summary(c) for c in matched_clubs],
        }
        for c in matched_clubs:
            room_id_by_club_id.setdefault(c.id, room_id)

    return rooms_by_id, room_id_by_club_id
