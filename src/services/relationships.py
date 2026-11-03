"""Derived housing links shared by profiles and graph; never stored as research."""

from collections import defaultdict
from itertools import combinations

from sqlalchemy import select

from db.models import (
    Location,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
)

ROOMMATE_NAMES = ("Coloc", "Colocataire")


async def roommate_links(db, person_id=None):
    rows = (
        await db.execute(
            select(PersonHousing.person_id, PersonHousing.location_id)
            .join(Person, Person.id == PersonHousing.person_id)
            .join(Location, Location.id == PersonHousing.location_id)
            .where(
                Person.kind == "STUDENT",
                PersonHousing.ended_at.is_(None),
                Location.kind == "APARTMENT",
                Location.is_active.is_(True),
            )
        )
    ).all()
    homes = defaultdict(set)
    for row in rows:
        homes[row.location_id].add(row.person_id)
    manual = (
        await db.execute(
            select(PersonRelationship.person_a_id, PersonRelationship.person_b_id)
            .join(
                RelationshipType,
                RelationshipType.id == PersonRelationship.relationship_type_id,
            )
            .where(RelationshipType.name.in_(ROOMMATE_NAMES))
        )
    ).all()
    explicit_pairs = {frozenset((row.person_a_id, row.person_b_id)) for row in manual}
    relation_type = (
        (
            await db.execute(
                select(RelationshipType).where(RelationshipType.name == "Coloc")
            )
        )
        .scalars()
        .first()
    )
    output = []
    for home, people in homes.items():
        for a, b in combinations(sorted(people, key=str), 2):
            if person_id is not None and person_id not in (a, b):
                continue
            if frozenset((a, b)) in explicit_pairs:
                continue
            output.append(
                {
                    "id": f"housing:{home}:{a}:{b}",
                    "source": str(a),
                    "target": str(b),
                    "location_id": str(home),
                    "kind": "relationship",
                    "origin": "housing",
                    "derived": True,
                    "directed": False,
                    "confidence": "CONFIRMED",
                    "label": relation_type.name if relation_type else "Coloc",
                    "color": relation_type.color if relation_type else "#f97316",
                    "relationship_type_id": str(relation_type.id)
                    if relation_type
                    else None,
                }
            )
    return output
