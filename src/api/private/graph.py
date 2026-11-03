from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.common import CLUB_KINDS, trombint_id_subquery
from api.private.deps import User, require_user
from db.database import get_db
from db.models import (
    Location,
    Organization,
    OrganizationMembership,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
)

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("")
async def get_full_graph(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Returns students, active clubs and class groups, and explicit links.

    NOTE: `Person` now also covers professors/alumni/staff/external subjects,
    but this graph historically only ever contained students (the old data
    model had no other kind of person). We keep filtering to
    `kind == "STUDENT"` here to preserve existing behavior; broadening the
    graph to other person kinds is a product decision for later.
    """
    student_res = await db.execute(
        select(
            Person.id,
            Person.first_name,
            Person.last_name,
            trombint_id_subquery(Person.id).label("trombint_id"),
            Person.profile_picture_path,
        ).where(Person.kind == "STUDENT")
    )
    students = student_res.all()
    student_ids = {s.id for s in students}

    club_res = await db.execute(
        select(
            Organization.id,
            Organization.name,
            Organization.logo_url,
            Organization.location_id,
            Organization.kind,
        ).where(
            Organization.kind.in_((*CLUB_KINDS, "CLASS_GROUP")),
            Organization.is_active.is_(True),
        )
    )
    clubs = club_res.all()

    sc_res = await db.execute(
        select(
            OrganizationMembership.id,
            OrganizationMembership.person_id,
            OrganizationMembership.organization_id,
            OrganizationMembership.role,
            Organization.kind,
        )
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            Organization.kind.in_((*CLUB_KINDS, "CLASS_GROUP")),
            Organization.is_active.is_(True),
            OrganizationMembership.ended_at.is_(None),
        )
    )
    student_clubs = [row for row in sc_res.all() if row.person_id in student_ids]

    rel_res = await db.execute(
        select(
            PersonRelationship.id,
            PersonRelationship.confidence,
            RelationshipType.is_symmetric,
            PersonRelationship.person_a_id,
            PersonRelationship.person_b_id,
            RelationshipType.name,
            RelationshipType.color,
        ).join(
            RelationshipType,
            PersonRelationship.relationship_type_id == RelationshipType.id,
        )
    )
    relationships = [
        row
        for row in rel_res.all()
        if row.person_a_id in student_ids and row.person_b_id in student_ids
    ]

    housing = (
        await db.execute(
            select(
                PersonHousing.person_id, Location.id, Location.code, Location.attributes
            )
            .join(Location, Location.id == PersonHousing.location_id)
            .where(PersonHousing.ended_at.is_(None), Location.is_active.is_(True))
        )
    ).all()
    housing_by_person = {row.person_id: row for row in housing}
    club_locations = (
        (
            await db.execute(
                select(Location).where(
                    Location.id.in_(
                        [club.location_id for club in clubs if club.location_id]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    locations = {location.id: location for location in club_locations}

    def map_context(attributes):
        geography = (attributes or {}).get("wemap", {})
        ids = geography.get("feature_ids", [])
        return {
            "feature": geography.get("primary_feature_id", ids[0] if ids else None),
            "building": geography.get("properties", {}).get("building_id"),
            "level": next(iter(geography.get("levels", [])), None),
        }

    nodes = []
    links = []

    for s in students:
        nodes.append(
            {
                "id": str(s.id),
                "name": f"{s.first_name} {s.last_name}",
                "group": "student",
                "trombint_id": s.trombint_id,
                "img": s.profile_picture_path,
                "apartment": housing_by_person[s.id].code
                if s.id in housing_by_person
                else None,
                "map": map_context(housing_by_person[s.id].attributes)
                if s.id in housing_by_person
                else None,
            }
        )

    for c in clubs:
        nodes.append(
            {
                "id": str(c.id),
                "name": c.name,
                "group": "club" if c.kind in CLUB_KINDS else "class_group",
                "img": c.logo_url,
                "room": locations[c.location_id].code
                if c.location_id in locations
                else None,
                "map": map_context(locations[c.location_id].attributes)
                if c.location_id in locations
                else None,
            }
        )

    for sc in student_clubs:
        links.append(
            {
                "id": f"membership:{sc.id}",
                "kind": "membership" if sc.kind in CLUB_KINDS else "class_group",
                "directed": False,
                "source": str(sc.person_id),
                "target": str(sc.organization_id),
                "label": sc.role,
                "color": "#3b82f6",
            }
        )

    for r in relationships:
        links.append(
            {
                "id": f"relationship:{r.id}",
                "kind": "relationship",
                "confidence": r.confidence,
                "directed": not r.is_symmetric,
                "source": str(r.person_a_id),
                "target": str(r.person_b_id),
                "label": r.name,
                "color": r.color,
            }
        )

    from services.relationships import roommate_links
    links.extend(await roommate_links(db))
    return {"nodes": nodes, "links": links}
