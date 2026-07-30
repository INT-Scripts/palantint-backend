from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.common import apartment_code_subquery, trombint_id_subquery
from api.private.deps import User, escape_like, require_user
from db.database import get_db
from db.models import Organization, Person

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
async def global_search(
    q: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user),
):
    if not q or len(q) < 2:
        return {"students": [], "clubs": [], "class_groups": [], "apartments": []}

    query_clean = q.strip().lower()
    search_terms = query_clean.split()

    # 1. SEARCH STUDENTS
    trombint_expr = trombint_id_subquery(Person.id)
    apartment_expr = apartment_code_subquery(Person.id)

    term_conditions = []
    for term in search_terms:
        t = escape_like(term)
        term_conditions.append(
            or_(
                func.unaccent(Person.first_name).ilike(func.unaccent(f"%{t}%")),
                func.unaccent(Person.last_name).ilike(func.unaccent(f"%{t}%")),
                func.unaccent(trombint_expr).ilike(f"%{t}%"),
                func.unaccent(apartment_expr).ilike(f"%{t}%")
            )
        )

    student_query = (
        select(Person, trombint_expr.label("trombint_id"), apartment_expr.label("apartment"))
        .where(Person.kind == "STUDENT", and_(*term_conditions))
        .limit(40)
    )
    student_res = await db.execute(student_query)
    students_raw = student_res.all()

    ranked_students = []
    for s, trombint_id, apartment in students_raw:
        score = 0
        fname = (s.first_name or "").lower()
        lname = (s.last_name or "").lower()
        tid = (trombint_id or "").lower()
        apt = (apartment or "").lower()

        if tid == query_clean:
            score += 100
        if apt == query_clean:
            score += 95

        matched_terms = 0
        for term in search_terms:
            term_score = 0
            if term == fname:
                term_score += 40
            elif fname.startswith(term):
                term_score += 20
            elif term and term in fname:
                term_score += 5

            if term == lname:
                term_score += 50
            elif lname.startswith(term):
                term_score += 30
            elif term and term in lname:
                term_score += 10

            if term_score > 0:
                score += term_score
                matched_terms += 1

        if len(search_terms) > 1 and matched_terms >= len(search_terms):
            score += 50

        full_name = f"{fname} {lname}"
        rev_name = f"{lname} {fname}"
        if query_clean in full_name:
            score += 40
        if query_clean in rev_name:
            score += 35

        ranked_students.append((score, s, trombint_id, apartment))

    ranked_students.sort(key=lambda x: x[0], reverse=True)

    # 2. SEARCH CLUBS
    club_query = select(Organization).where(
        Organization.kind.in_(("CLUB", "BUREAU")),
        or_(
            func.unaccent(Organization.name).ilike(func.unaccent(f"%{escape_like(query_clean)}%")),
            func.unaccent(Organization.slug).ilike(f"%{escape_like(query_clean)}%")
        )
    ).limit(10)
    club_res = await db.execute(club_query)
    clubs_raw = club_res.scalars().all()

    ranked_clubs = []
    for c in clubs_raw:
        score = 0
        name = (c.name or "").lower()
        slug = (c.slug or "").lower()
        if name == query_clean:
            score += 100
        if slug == query_clean:
            score += 90
        if name.startswith(query_clean):
            score += 50
        ranked_clubs.append((score, c))
    ranked_clubs.sort(key=lambda x: x[0], reverse=True)

    # 3. SEARCH CLASS GROUPS
    class_query = select(Organization).where(
        Organization.kind == "CLASS_GROUP",
        func.unaccent(Organization.name).ilike(func.unaccent(f"%{escape_like(query_clean)}%"))
    ).limit(10)
    class_res = await db.execute(class_query)
    classes_raw = class_res.scalars().all()

    ranked_classes = []
    for cg in classes_raw:
        score = 0
        name = (cg.name or "").lower()
        if name == query_clean:
            score += 100
        if name.startswith(query_clean):
            score += 50
        ranked_classes.append((score, cg))
    ranked_classes.sort(key=lambda x: x[0], reverse=True)

    return {
        "students": [
            {
                "id": str(s.id),
                "first_name": s.first_name,
                "last_name": s.last_name,
                "trombint_id": trombint_id,
                "apartment": apartment
            } for score, s, trombint_id, apartment in ranked_students[:15]
        ],
        "clubs": [
            {
                "id": str(c.id),
                "name": c.name,
                "slug": c.slug,
                "logo_url": c.logo_url
            } for score, c in ranked_clubs[:5]
        ],
        "class_groups": [
            {
                "id": str(cg.id),
                "name": cg.name
            } for score, cg in ranked_classes[:5]
        ],
        "apartments": [
            {
                "apartment_id": apartment,
                "student_id": str(s.id),
                "student_name": f"{s.first_name} {s.last_name}"
            } for score, s, trombint_id, apartment in ranked_students if apartment and (
                any(term and term in apartment.lower() for term in search_terms)
            )
        ][:5]
    }
