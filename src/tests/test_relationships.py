import pytest
from sqlalchemy.future import select

from conftest import make_student
from db.models import PersonRelationship, RelationshipType


@pytest.mark.asyncio
async def test_create_relationship_defaults_confidence_and_omits_evidence(client, db_session, admin_token, trombint_source):
    student_a = await make_student(db_session, trombint_source, "Alice", "Alpha", "aalpha")
    student_b = await make_student(db_session, trombint_source, "Bob", "Beta", "bbeta")
    rel_type = RelationshipType(name="Friends", color="#ff0000")
    db_session.add(rel_type)
    await db_session.commit()
    await db_session.refresh(rel_type)

    headers = {"Authorization": f"Bearer {admin_token}"}
    res = await client.post(
        "/api/private/relationships",
        json={
            "student_a_id": str(student_a.id),
            "student_b_id": str(student_b.id),
            "relationship_type_id": str(rel_type.id),
        },
        headers=headers,
    )
    assert res.status_code == 200

    # The create schema (RelationshipCreate) has no confidence/evidence_media_id
    # fields at all, so there's no way to set them via this endpoint today --
    # confirm the model default ("LIKELY") is what lands in the DB, and
    # evidence_media_id stays unset.
    result = await db_session.execute(select(PersonRelationship))
    rel = result.scalars().first()
    assert rel.confidence == "LIKELY"
    assert rel.evidence_media_id is None

    # And confirm current GET behavior: confidence/evidence_media_id are NOT
    # surfaced in the relationships list response (per the admitted open
    # product decision documented in api/private/relationships.py).
    list_res = await client.get(f"/api/private/students/{student_a.id}/relationships", headers=headers)
    assert list_res.status_code == 200
    body = list_res.json()
    assert len(body) == 1
    assert "confidence" not in body[0]
    assert "evidence_media_id" not in body[0]


@pytest.mark.asyncio
async def test_relationships_requires_admin_to_create(client, db_session, user_token, trombint_source):
    student_a = await make_student(db_session, trombint_source, "Alice", "Alpha", "aalpha")
    student_b = await make_student(db_session, trombint_source, "Bob", "Beta", "bbeta")
    rel_type = RelationshipType(name="Friends", color="#ff0000")
    db_session.add(rel_type)
    await db_session.commit()
    await db_session.refresh(rel_type)

    headers = {"Authorization": f"Bearer {user_token}"}
    res = await client.post(
        "/api/private/relationships",
        json={
            "student_a_id": str(student_a.id),
            "student_b_id": str(student_b.id),
            "relationship_type_id": str(rel_type.id),
        },
        headers=headers,
    )
    assert res.status_code == 403
