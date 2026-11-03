import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.common import get_active_promo_school, get_trombint_id
from api.private.deps import User, require_admin, require_user
from db.database import get_db
from db.models import Person, PersonRelationship, RelationshipType

router = APIRouter(tags=["relationships"])


class RelationshipCreate(BaseModel):
    # Field names kept as student_a_id/student_b_id (rather than the new
    # person_a_id/person_b_id) to match the existing frontend request shape.
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
        select(PersonRelationship)
        .options(
            selectinload(PersonRelationship.person_a),
            selectinload(PersonRelationship.person_b),
            selectinload(PersonRelationship.relationship_type),
        )
        .where(
            or_(
                PersonRelationship.person_a_id == student_id,
                PersonRelationship.person_b_id == student_id,
            )
        )
    )
    rels = result.scalars().all()
    output = []
    for rel in rels:
        other = (
            rel.person_b if str(rel.person_a_id) == str(student_id) else rel.person_a
        )
        if other:
            other_trombint_id = await get_trombint_id(db, other.id)
            other_promo, _other_school = await get_active_promo_school(db, other.id)
            output.append(
                {
                    "id": str(rel.id),
                    "derived": False,
                    # confidence/evidence_media_id exist on PersonRelationship
                    # but are not surfaced here yet — open product decision.
                    "other_student": {
                        "id": str(other.id),
                        "first_name": other.first_name,
                        "last_name": other.last_name,
                        "trombint_id": other_trombint_id,
                        "promo": other_promo,
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

    from services.relationships import roommate_links

    for link in await roommate_links(db, student_id):
        other_id = uuid.UUID(
            link["target"] if link["source"] == str(student_id) else link["source"]
        )
        other = await db.get(Person, other_id)
        if other is None:
            continue
        promo, _ = await get_active_promo_school(db, other.id)
        output.append(
            {
                "id": link["id"],
                "derived": True,
                "origin": "housing",
                "location_id": link["location_id"],
                "other_student": {
                    "id": str(other.id),
                    "first_name": other.first_name,
                    "last_name": other.last_name,
                    "trombint_id": await get_trombint_id(db, other.id),
                    "promo": promo,
                    "profile_picture_path": other.profile_picture_path,
                },
                "relationship_type": {
                    "id": link["relationship_type_id"],
                    "name": link["label"],
                    "color": link["color"],
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
    rel = PersonRelationship(
        person_a_id=data.student_a_id,
        person_b_id=data.student_b_id,
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
    rel = await db.get(PersonRelationship, rel_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relationship not found")
    await db.delete(rel)
    await db.commit()
    return {"status": "deleted"}
