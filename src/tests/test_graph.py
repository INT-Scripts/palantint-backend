import uuid

import pytest

from conftest import make_student
from db.models import Organization, OrganizationMembership, Person, PersonRelationship, RelationshipType


@pytest.mark.asyncio
async def test_graph_only_includes_student_persons_and_club_organizations(client, db_session, user_token, trombint_source):
    student = await make_student(db_session, trombint_source, "Alice", "Alpha", "aalpha")

    # A non-STUDENT Person (e.g. a professor) must not appear as a node.
    professor = Person(id=uuid.uuid4(), kind="PROFESSOR", first_name="Prof", last_name="Essor")
    db_session.add(professor)

    club = Organization(id=uuid.uuid4(), kind="CLUB", name="Robotics", slug="robotics")
    # A non-CLUB Organization (e.g. a PROMO) must not appear as a node.
    promo = Organization(id=uuid.uuid4(), kind="PROMO", name="Promo2026")
    db_session.add(club)
    db_session.add(promo)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/graph", headers=headers)
    assert response.status_code == 200
    data = response.json()

    node_ids = {n["id"] for n in data["nodes"]}
    assert str(student.id) in node_ids
    assert str(club.id) in node_ids
    assert str(professor.id) not in node_ids
    assert str(promo.id) not in node_ids

    groups = {n["group"] for n in data["nodes"]}
    assert groups <= {"student", "club"}


@pytest.mark.asyncio
async def test_graph_relationship_to_filtered_out_person_produces_no_dangling_edge(client, db_session, user_token, trombint_source):
    student = await make_student(db_session, trombint_source, "Alice", "Alpha", "aalpha")
    professor = Person(id=uuid.uuid4(), kind="PROFESSOR", first_name="Prof", last_name="Essor")
    db_session.add(professor)
    rel_type = RelationshipType(name="Advisor", color="#123456")
    db_session.add(rel_type)
    await db_session.flush()

    db_session.add(
        PersonRelationship(
            person_a_id=student.id, person_b_id=professor.id, relationship_type_id=rel_type.id
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/graph", headers=headers)
    assert response.status_code == 200
    data = response.json()

    node_ids = {n["id"] for n in data["nodes"]}
    assert str(professor.id) not in node_ids

    # No link should reference the filtered-out professor as source or target.
    for link in data["links"]:
        assert link["source"] != str(professor.id)
        assert link["target"] != str(professor.id)


@pytest.mark.asyncio
async def test_graph_includes_club_membership_and_student_relationship_edges(client, db_session, user_token, trombint_source):
    student_a = await make_student(db_session, trombint_source, "Alice", "Alpha", "aalpha")
    student_b = await make_student(db_session, trombint_source, "Bob", "Beta", "bbeta")
    club = Organization(id=uuid.uuid4(), kind="CLUB", name="Robotics", slug="robotics")
    db_session.add(club)
    await db_session.flush()
    db_session.add(
        OrganizationMembership(person_id=student_a.id, organization_id=club.id, role="Membre")
    )
    rel_type = RelationshipType(name="Friends", color="#123456")
    db_session.add(rel_type)
    await db_session.flush()
    db_session.add(
        PersonRelationship(
            person_a_id=student_a.id, person_b_id=student_b.id, relationship_type_id=rel_type.id
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/graph", headers=headers)
    data = response.json()

    membership_links = [
        l for l in data["links"] if l["source"] == str(student_a.id) and l["target"] == str(club.id)
    ]
    assert len(membership_links) == 1

    rel_links = [
        l for l in data["links"] if l["source"] == str(student_a.id) and l["target"] == str(student_b.id)
    ]
    assert len(rel_links) == 1
