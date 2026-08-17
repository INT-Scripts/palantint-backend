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

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.config import settings
from db.models import DataSource, ExternalIdentity, Location, Organization, OrganizationMembership, PersonHousing
# Re-exported: the API must match on exactly the key the ETL loaders write.
from db.naming import person_name_key

TROMBINT_SOURCE_CODE = "trombint"
CLUB_KINDS = ("CLUB", "BUREAU")


async def get_data_source_id(db: AsyncSession, code: str) -> Optional[uuid.UUID]:
    result = await db.execute(select(DataSource.id).where(DataSource.code == code))
    return result.scalar_one_or_none()


# ── Name-based person resolution ─────────────────────────────────────────────
# Some sources name people without any usable identifier — course sheets list
# "Jean-Pierre DURAND", never a trombint uid. Rather than resolving such names
# once at ingest time (which leaves the row unlinked forever if the Person is
# created later), they are stored with a normalized key and matched at read
# time, so adding the Person afterwards links them retroactively.

async def resolve_people_by_name_keys(
    db: AsyncSession, name_keys: set[str]
) -> Dict[str, Any]:
    """Map each name key to its Person, for the keys that match exactly one.

    A key matching several people (homonyms) is deliberately left out: showing
    no link is better than linking a course to the wrong person.
    """
    from db.models import Person  # local import: avoids a cycle at module load

    wanted = {k for k in name_keys if k}
    if not wanted:
        return {}

    tokens = {token for key in wanted for token in key.split(" ")}
    if not tokens:
        return {}

    # Prefilter in SQL on any single token (`people` is campus-sized), then
    # compare full keys in Python — the sorted-token key cannot be expressed
    # portably in SQL.
    result = await db.execute(
        select(Person).where(
            or_(
                *[
                    func.unaccent(func.lower(Person.last_name)).like(f"%{token}%")
                    for token in tokens
                ]
            )
        )
    )

    matches: Dict[str, list] = {}
    for person in result.scalars().all():
        key = person_name_key(person.first_name, person.last_name, person.display_name)
        if key in wanted:
            matches.setdefault(key, []).append(person)
        else:
            # display_name can hold a different spelling than first+last.
            alt = person_name_key(person.first_name, person.last_name)
            if alt in wanted:
                matches.setdefault(alt, []).append(person)

    return {key: people[0] for key, people in matches.items() if len(people) == 1}


async def get_taught_courses(db: AsyncSession, person: Any) -> List[Dict[str, Any]]:
    """Courses whose sheet names this person, matched on `name_key` — the
    reverse of resolve_people_by_name_keys, and an indexed exact lookup."""
    from db.models import Course, CourseTeacher

    keys = {
        person_name_key(person.first_name, person.last_name),
        person_name_key(person.display_name),
    } - {""}
    if not keys:
        return []

    result = await db.execute(
        select(Course, CourseTeacher.role)
        .join(CourseTeacher, CourseTeacher.course_id == Course.id)
        .where(CourseTeacher.name_key.in_(keys), Course.is_active.is_(True))
        .order_by(Course.title)
    )

    courses = []
    for course, role in result.all():
        courses.append(
            {
                "id": str(course.id),
                "code": course.code,
                "title": course.title,
                "role": role,
                "schools": course.schools or [],
                "credits_ects": course.credits_ects,
                "niveau": course.niveau,
                "domaine": course.domaine,
            }
        )
    return courses


def serialize_linked_person(person: Any) -> Dict[str, Any]:
    return {
        "id": str(person.id),
        "first_name": person.first_name,
        "last_name": person.last_name,
        "kind": person.kind,
    }


# ── Course catalog (shared between the public and private routers) ───────────
# Both spaces serve the same scraped catalog; only the private one resolves
# teachers to Person profiles (the public space never exposes the identity
# graph — see AGENTS.md "Séparation Strictement Imposée Backend").

COURSE_SCHOOL_LABELS = {
    "tsp": "Télécom SudParis",
    "imt-bs": "IMT-BS",
    "lsh": "Langues & Sciences Humaines",
}


def course_school_filter(school: str):
    """`Course.schools` is a JSON array, and the generic JSON type has no
    portable containment operator (JSONB's `@>` is Postgres-only and would
    break the SQLite test engine). Matching the serialized array is enough:
    school codes are short slugs that can't collide with other content."""
    from api.private.deps import escape_like
    from db.models import Course

    return cast(Course.schools, Text).ilike(f'%"{escape_like(school)}"%')


def course_multi_value_filter(column, value: str):
    """Some fiche fields hold several values in one comma-separated string
    ("P1,P2", "Evry,En ligne", "Français/French,Anglais/English"), so a course
    tagged "P1,P2" must match a "P1" filter. Note that the separator is a bare
    comma: `domaine` labels such as "Marketing, commercial" use a comma *plus a
    space* and are single values — never filtered through here."""
    from api.private.deps import escape_like

    escaped = escape_like(value)
    return or_(
        column == value,
        column.like(f"{escaped},%"),
        column.like(f"%,{escaped}"),
        column.like(f"%,{escaped},%"),
    )


def split_course_multi_values(raw_values) -> List[str]:
    return sorted(
        {
            part.strip()
            for raw in raw_values
            if raw
            for part in str(raw).split(",")
            if part.strip()
        }
    )


def build_course_query(
    q: Optional[str] = None,
    school: Optional[str] = None,
    niveau: Optional[str] = None,
    domaine: Optional[str] = None,
    langue: Optional[str] = None,
    periode: Optional[str] = None,
    lieu: Optional[str] = None,
):
    from api.private.deps import escape_like
    from db.models import Course

    query = select(Course).where(Course.is_active.is_(True))

    if q:
        pattern = func.unaccent(f"%{escape_like(q)}%")
        query = query.where(
            or_(
                func.unaccent(Course.title).ilike(pattern),
                func.unaccent(Course.code).ilike(pattern),
                func.unaccent(Course.domaine).ilike(pattern),
                func.unaccent(Course.objectif).ilike(pattern),
                func.unaccent(Course.introduction).ilike(pattern),
            )
        )

    if school:
        query = query.where(course_school_filter(school))
    if niveau:
        query = query.where(Course.niveau == niveau)
    if domaine:
        query = query.where(Course.domaine == domaine)
    if langue:
        query = query.where(course_multi_value_filter(Course.langue_enseignement, langue))
    if periode:
        query = query.where(course_multi_value_filter(Course.periode, periode))
    if lieu:
        query = query.where(course_multi_value_filter(Course.lieu, lieu))

    return query


async def course_filter_facets(db: AsyncSession) -> Dict[str, Any]:
    """Distinct facet values for the catalog filter panel."""
    from db.models import Course

    async def raw_values(column) -> List[str]:
        result = await db.execute(
            select(column).where(Course.is_active.is_(True), column.is_not(None)).distinct()
        )
        return [v for v in result.scalars().all() if v and v.strip()]

    async def distinct(column) -> List[str]:
        return sorted({v.strip() for v in await raw_values(column)})

    async def distinct_multi(column) -> List[str]:
        # Facets whose column can hold a comma-separated list.
        return split_course_multi_values(await raw_values(column))

    schools_result = await db.execute(select(Course.schools).where(Course.is_active.is_(True)))
    school_codes = sorted({s for row in schools_result.scalars().all() for s in (row or [])})

    departements_result = await db.execute(
        select(Course.departements).where(Course.is_active.is_(True))
    )

    return {
        "schools": [
            {"code": code, "label": COURSE_SCHOOL_LABELS.get(code, code)}
            for code in school_codes
        ],
        "niveaux": await distinct(Course.niveau),
        "domaines": await distinct(Course.domaine),
        "langues": await distinct_multi(Course.langue_enseignement),
        "periodes": await distinct_multi(Course.periode),
        "lieux": await distinct_multi(Course.lieu),
        "departements": sorted(
            {
                d.strip()
                for row in departements_result.scalars().all()
                for d in (row or [])
                if d and d.strip()
            }
        ),
    }


def serialize_course_teacher(
    teacher: Any, person: Optional[Any] = None, with_person: bool = False
) -> Dict[str, Any]:
    data = {"name": teacher.name, "role": teacher.role, "url": teacher.url}
    if with_person:
        data["person"] = serialize_linked_person(person) if person else None
    return data


def serialize_course_summary(course: Any, with_teachers: bool = True) -> Dict[str, Any]:
    data = {
        "id": str(course.id),
        "external_id": course.external_id,
        "code": course.code,
        "title": course.title,
        "url": course.url,
        "schools": course.schools or [],
        "niveau": course.niveau,
        "graduate": course.graduate,
        "domaine": course.domaine,
        "programme": course.programme,
        "langue_enseignement": course.langue_enseignement,
        "periode": course.periode,
        "lieu": course.lieu,
        "credits_ects": course.credits_ects,
        "heures_programmees": course.heures_programmees,
        "departements": course.departements or [],
    }
    if with_teachers:
        data["responsables"] = [
            {"name": t.name} for t in course.teachers if t.role == "RESPONSABLE"
        ]
    return data


def serialize_course_details(
    course: Any, people_by_key: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """`people_by_key` is only passed by the private router: the public space
    serves the names as published, never a link into the identity graph."""
    with_person = people_by_key is not None
    people_by_key = people_by_key or {}

    def teachers(role: str) -> List[Dict[str, Any]]:
        return [
            serialize_course_teacher(t, people_by_key.get(t.name_key), with_person)
            for t in course.teachers
            if t.role == role
        ]

    return {
        **serialize_course_summary(course, with_teachers=False),
        "school_labels": [
            COURSE_SCHOOL_LABELS.get(code, code) for code in (course.schools or [])
        ],
        "responsables": teachers("RESPONSABLE"),
        "equipe_pedagogique": teachers("TEACHING_TEAM"),
        "coefficient": course.coefficient,
        "organisation": course.organisation,
        "population": course.population,
        "mode_calcul_moyenne": course.mode_calcul_moyenne,
        "mode_calcul_credits": course.mode_calcul_credits,
        "introduction": course.introduction,
        "objectif": course.objectif,
        "contenu": course.contenu,
        "evaluations": course.evaluations,
        "plan_cours": course.plan_cours,
        "charge_travail_etudiant": course.charge_travail_etudiant,
        "description": course.description,
        # Optional fiche sections (prerequis, bibliographie, motcles…): shape
        # varies from one course to the next, rendered generically.
        "extra_sections": course.attributes or {},
        "last_seen_at": course.last_seen_at.isoformat() if course.last_seen_at else None,
    }


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
