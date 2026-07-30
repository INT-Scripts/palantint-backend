from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.common import trombint_id_subquery
from api.private.deps import User, require_user
from db.database import get_db
from db.models import Organization, OrganizationMembership, Person, PersonRelationship, RelationshipType

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("")
async def get_full_graph(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Returns all students, clubs, and their relationships as a graph structure.

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
        select(Organization.id, Organization.name, Organization.logo_url).where(Organization.kind == "CLUB")
    )
    clubs = club_res.all()

    sc_res = await db.execute(
        select(OrganizationMembership.person_id, OrganizationMembership.organization_id, OrganizationMembership.role)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(Organization.kind == "CLUB", OrganizationMembership.ended_at.is_(None))
    )
    student_clubs = [row for row in sc_res.all() if row.person_id in student_ids]

    rel_res = await db.execute(
        select(
            PersonRelationship.person_a_id,
            PersonRelationship.person_b_id,
            RelationshipType.name,
            RelationshipType.color,
        ).join(RelationshipType, PersonRelationship.relationship_type_id == RelationshipType.id)
    )
    # Confidence/evidence_media_id are available on PersonRelationship but not
    # surfaced here yet — open product decision, see PROMPT.md.
    relationships = [
        row for row in rel_res.all() if row.person_a_id in student_ids and row.person_b_id in student_ids
    ]

    nodes = []
    links = []

    for s in students:
        nodes.append({
            "id": str(s.id),
            "name": f"{s.first_name} {s.last_name}",
            "group": "student",
            "trombint_id": s.trombint_id,
            "img": s.profile_picture_path
        })

    for c in clubs:
        nodes.append({
            "id": str(c.id),
            "name": c.name,
            "group": "club",
            "img": c.logo_url
        })

    for sc in student_clubs:
        links.append({
            "source": str(sc.person_id),
            "target": str(sc.organization_id),
            "label": sc.role,
            "color": "#3b82f6"
        })

    for r in relationships:
        links.append({
            "source": str(r.person_a_id),
            "target": str(r.person_b_id),
            "label": r.name,
            "color": r.color
        })

    return {"nodes": nodes, "links": links}
