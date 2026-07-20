import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.private.deps import User, require_admin, require_user
from db.database import get_db
from db.models import RelationshipType, Student, StudentRelationship

router = APIRouter(tags=["relationships"])


class RelationshipCreate(BaseModel):
    student_a_id: uuid.UUID
    student_b_id: uuid.UUID
    relationship_type_id: uuid.UUID


@router.get("/relationship-types")
async def get_relationship_types(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(select(RelationshipType))
    return result.scalars().all()


@router.get("/students/{student_id}/relationships")
async def get_student_relationships(
    student_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    result = await db.execute(
        select(StudentRelationship)
        .options(
            selectinload(StudentRelationship.student_a),
            selectinload(StudentRelationship.student_b),
            selectinload(StudentRelationship.relationship_type),
        )
        .where(
            or_(
                StudentRelationship.student_a_id == student_id,
                StudentRelationship.student_b_id == student_id,
            )
        )
    )
    rels = result.scalars().all()
    output = []
    for rel in rels:
        other = (
            rel.student_b if str(rel.student_a_id) == str(student_id) else rel.student_a
        )
        if other:
            output.append(
                {
                    "id": str(rel.id),
                    "other_student": {
                        "id": str(other.id),
                        "first_name": other.first_name,
                        "last_name": other.last_name,
                        "trombint_id": other.trombint_id,
                        "promo": other.promo,
                        "profile_picture_path": other.profile_picture_path,
                    },
                    "relationship_type": {
                        "id": str(rel.relationship_type.id),
                        "name": rel.relationship_type.name,
                        "color": rel.relationship_type.color,
                    },
                    "created_at": str(rel.created_at),
                }
            )

    # Dynamically fetch colocataires (roommates)
    student = await db.get(Student, student_id)
    if student and student.apartment:
        apt = student.apartment.strip()
        roommates_query = select(Student).where(
            Student.apartment == apt, Student.id != student_id
        )
        roomates_result = await db.execute(roommates_query)
        roommates = roomates_result.scalars().all()

        for rm in roommates:
            output.append(
                {
                    "id": f"coloc-{rm.id}",
                    "other_student": {
                        "id": str(rm.id),
                        "first_name": rm.first_name,
                        "last_name": rm.last_name,
                        "trombint_id": rm.trombint_id,
                        "promo": rm.promo,
                        "profile_picture_path": rm.profile_picture_path,
                    },
                    "relationship_type": {
                        "id": "builtin-colocataire",
                        "name": "Colocataire",
                        "color": "#f97316",  # Orange-500
                    },
                    "created_at": None,
                }
            )

    return output


@router.post("/relationships")
async def create_relationship(
    data: RelationshipCreate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rel = StudentRelationship(
        student_a_id=data.student_a_id,
        student_b_id=data.student_b_id,
        relationship_type_id=data.relationship_type_id,
    )
    db.add(rel)
    await db.commit()
    await db.refresh(rel)
    return rel


@router.delete("/relationships/{rel_id}")
async def delete_relationship(
    rel_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rel = await db.get(StudentRelationship, rel_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relationship not found")
    await db.delete(rel)
    await db.commit()
    return {"status": "deleted"}
