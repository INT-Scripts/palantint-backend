from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from db.database import get_db
from db.models import Student, Club, StudentClub, StudentRelationship, RelationshipType

router = APIRouter(prefix="/graph", tags=["graph"])

@router.get("")
async def get_full_graph(db: AsyncSession = Depends(get_db)):
    """Returns all students, clubs, and their relationships as a graph structure."""
    # 1. Fetch Students
    student_res = await db.execute(select(Student.id, Student.first_name, Student.last_name, Student.trombint_id, Student.profile_picture_path))
    students = student_res.all()

    # 2. Fetch Clubs
    club_res = await db.execute(select(Club.id, Club.name, Club.logo_url))
    clubs = club_res.all()

    # 3. Fetch Student-Club relations
    sc_res = await db.execute(select(StudentClub.student_id, StudentClub.club_id, StudentClub.role))
    student_clubs = sc_res.all()

    # 4. Fetch Student-Student relations
    rel_res = await db.execute(
        select(StudentRelationship.student_a_id, StudentRelationship.student_b_id, RelationshipType.name, RelationshipType.color)
        .join(RelationshipType, StudentRelationship.relationship_type_id == RelationshipType.id)
    )
    relationships = rel_res.all()

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
            "source": str(sc.student_id),
            "target": str(sc.club_id),
            "label": sc.role,
            "color": "#3b82f6" # default club link color
        })

    for r in relationships:
        links.append({
            "source": str(r.student_a_id),
            "target": str(r.student_b_id),
            "label": r.name,
            "color": r.color
        })

    return {"nodes": nodes, "links": links}
