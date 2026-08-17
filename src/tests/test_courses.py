import uuid

import pytest
import pytest_asyncio

from db.models import Course, CourseTeacher, DataSource, Person
from db.naming import person_name_key


@pytest_asyncio.fixture
async def intllabus_source(db_session):
    source = DataSource(
        id=uuid.uuid4(), code="intllabus", kind="SCRAPER", label="Catalogue des cours"
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


async def make_course(db_session, source, **overrides):
    fields = {
        "id": uuid.uuid4(),
        "source_id": source.id,
        "external_id": str(uuid.uuid4().int)[:5],
        "code": "CSC 4101",
        "title": "Réseaux avancés",
        "schools": ["tsp"],
        "niveau": "M1",
        "domaine": "Réseaux",
        "langue_enseignement": "Français/French",
        "periode": "P1",
        "lieu": "Evry",
        "credits_ects": 2.5,
        "departements": ["Réseaux et Services de Télécommunications"],
    }
    teachers = overrides.pop(
        "teachers", [("RESPONSABLE", "Ada Lovelace"), ("TEACHING_TEAM", "Grace HOPPER")]
    )
    fields.update(overrides)
    course = Course(**fields)
    db_session.add(course)
    await db_session.flush()
    for position, (role, name) in enumerate(teachers):
        db_session.add(
            CourseTeacher(
                course_id=course.id,
                role=role,
                name=name,
                name_key=person_name_key(name),
                position=position,
            )
        )
    await db_session.commit()
    await db_session.refresh(course)
    return course


@pytest.mark.asyncio
async def test_courses_requires_authentication(client):
    response = await client.get("/api/private/courses")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_courses_list_paginates_and_hides_inactive(client, db_session, user_token, intllabus_source):
    await make_course(db_session, intllabus_source, external_id="1", title="Algèbre")
    await make_course(db_session, intllabus_source, external_id="2", title="Byzantine Systems")
    await make_course(
        db_session, intllabus_source, external_id="3", title="Cours supprimé", is_active=False
    )

    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get("/api/private/courses?limit=1", headers=headers)
    assert response.status_code == 200
    data = response.json()

    # `total` counts every match, not just the current page.
    assert data["total"] == 2
    assert len(data["courses"]) == 1
    assert data["courses"][0]["title"] == "Algèbre"

    page_two = await client.get("/api/private/courses?limit=1&skip=1", headers=headers)
    assert page_two.json()["courses"][0]["title"] == "Byzantine Systems"

    titles = {c["title"] for c in (await client.get("/api/private/courses", headers=headers)).json()["courses"]}
    assert "Cours supprimé" not in titles


@pytest.mark.asyncio
async def test_courses_search_and_facet_filters(client, db_session, user_token, intllabus_source):
    await make_course(
        db_session, intllabus_source, external_id="1", title="Réseaux avancés", niveau="M1"
    )
    await make_course(
        db_session,
        intllabus_source,
        external_id="2",
        title="Marketing digital",
        code="MKG 1002",
        niveau="L3",
        domaine="Marketing, commercial",
        schools=["imt-bs"],
    )

    headers = {"Authorization": f"Bearer {user_token}"}

    by_title = await client.get("/api/private/courses?q=marketing", headers=headers)
    assert [c["code"] for c in by_title.json()["courses"]] == ["MKG 1002"]

    by_code = await client.get("/api/private/courses?q=CSC", headers=headers)
    assert [c["title"] for c in by_code.json()["courses"]] == ["Réseaux avancés"]

    by_niveau = await client.get("/api/private/courses?niveau=L3", headers=headers)
    assert by_niveau.json()["total"] == 1

    # `schools` is a JSON array — the school facet must match one entry of it.
    by_school = await client.get("/api/private/courses?school=imt-bs", headers=headers)
    assert [c["code"] for c in by_school.json()["courses"]] == ["MKG 1002"]

    empty = await client.get("/api/private/courses?school=lsh", headers=headers)
    assert empty.json()["total"] == 0


@pytest.mark.asyncio
async def test_comma_separated_facets_match_each_value(client, db_session, user_token, intllabus_source):
    # A fiche can carry several values in one string; each must be filterable.
    await make_course(
        db_session,
        intllabus_source,
        external_id="1",
        title="Cours bi-période",
        periode="P1,P2",
        lieu="Evry,En ligne",
        langue_enseignement="Français/French,Anglais/English",
    )
    await make_course(
        db_session, intllabus_source, external_id="2", title="Cours P3", periode="P3", lieu="Paris"
    )

    headers = {"Authorization": f"Bearer {user_token}"}

    for query in ["periode=P1", "periode=P2", "lieu=En%20ligne", "langue=Anglais/English"]:
        response = await client.get(f"/api/private/courses?{query}", headers=headers)
        assert [c["title"] for c in response.json()["courses"]] == ["Cours bi-période"], query

    # Composite values must not leak into the facet lists as single options.
    facets = (await client.get("/api/private/courses/filters", headers=headers)).json()
    assert facets["periodes"] == ["P1", "P2", "P3"]
    assert facets["lieux"] == ["En ligne", "Evry", "Paris"]
    assert facets["langues"] == ["Anglais/English", "Français/French"]

    # `domaine` labels legitimately contain ", " and stay whole.
    await make_course(
        db_session, intllabus_source, external_id="3", domaine="Marketing, commercial"
    )
    facets = (await client.get("/api/private/courses/filters", headers=headers)).json()
    assert "Marketing, commercial" in facets["domaines"]


@pytest.mark.asyncio
async def test_course_filters_lists_distinct_facets(client, db_session, user_token, intllabus_source):
    await make_course(db_session, intllabus_source, external_id="1")
    await make_course(
        db_session,
        intllabus_source,
        external_id="2",
        niveau="L3",
        domaine="Management",
        schools=["imt-bs", "lsh"],
    )
    await make_course(
        db_session, intllabus_source, external_id="3", niveau="PhD", is_active=False
    )

    headers = {"Authorization": f"Bearer {user_token}"}
    data = (await client.get("/api/private/courses/filters", headers=headers)).json()

    assert data["niveaux"] == ["L3", "M1"]  # inactive course excluded
    assert data["domaines"] == ["Management", "Réseaux"]
    assert [s["code"] for s in data["schools"]] == ["imt-bs", "lsh", "tsp"]
    assert {"code": "tsp", "label": "Télécom SudParis"} in data["schools"]
    assert data["departements"] == ["Réseaux et Services de Télécommunications"]


@pytest.mark.asyncio
async def test_course_details_exposes_full_sheet(client, db_session, user_token, intllabus_source):
    course = await make_course(
        db_session,
        intllabus_source,
        external_id="1",
        objectif="Comprendre les réseaux",
        evaluations="Examen final",
        attributes={"prerequis": "Algèbre linéaire", "motcles": "TCP, IP"},
    )

    headers = {"Authorization": f"Bearer {user_token}"}
    response = await client.get(f"/api/private/courses/{course.id}", headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert data["objectif"] == "Comprendre les réseaux"
    assert data["evaluations"] == "Examen final"
    assert data["equipe_pedagogique"][0]["name"] == "Grace HOPPER"
    assert data["responsables"][0]["name"] == "Ada Lovelace"
    assert data["extra_sections"]["prerequis"] == "Algèbre linéaire"
    assert data["school_labels"] == ["Télécom SudParis"]


@pytest.mark.asyncio
async def test_course_details_404_on_unknown_and_inactive(client, db_session, user_token, intllabus_source):
    headers = {"Authorization": f"Bearer {user_token}"}

    unknown = await client.get(f"/api/private/courses/{uuid.uuid4()}", headers=headers)
    assert unknown.status_code == 404

    inactive = await make_course(db_session, intllabus_source, external_id="1", is_active=False)
    response = await client.get(f"/api/private/courses/{inactive.id}", headers=headers)
    assert response.status_code == 404


# ── Teacher <-> Person links ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_teacher_links_appear_when_the_person_is_added_later(
    client, db_session, user_token, intllabus_source
):
    """The whole point of matching at read time: a course scraped before the
    person existed must link to them as soon as they are created."""
    course = await make_course(
        db_session,
        intllabus_source,
        external_id="1",
        teachers=[("RESPONSABLE", "Jean-Pierre DURAND")],
    )
    headers = {"Authorization": f"Bearer {user_token}"}

    before = await client.get(f"/api/private/courses/{course.id}", headers=headers)
    assert before.json()["responsables"][0]["person"] is None

    # Person created afterwards — no re-scrape, no re-load, no resolution pass.
    professor = Person(
        id=uuid.uuid4(), kind="PROFESSOR", first_name="Jean-Pierre", last_name="Durand"
    )
    db_session.add(professor)
    await db_session.commit()

    after = await client.get(f"/api/private/courses/{course.id}", headers=headers)
    linked = after.json()["responsables"][0]
    assert linked["name"] == "Jean-Pierre DURAND"  # published spelling is kept
    assert linked["person"]["id"] == str(professor.id)


@pytest.mark.asyncio
async def test_teacher_matching_ignores_case_accents_and_name_order(
    client, db_session, user_token, intllabus_source
):
    course = await make_course(
        db_session,
        intllabus_source,
        external_id="1",
        # Compound surname, written "Prénom NOM" by the catalog.
        teachers=[("RESPONSABLE", "Emelina CUCUNUBA BARRERA")],
    )
    person = Person(
        id=uuid.uuid4(),
        kind="PROFESSOR",
        first_name="Émelina",
        last_name="Cucunuba Barrera",
    )
    db_session.add(person)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {user_token}"}
    data = (await client.get(f"/api/private/courses/{course.id}", headers=headers)).json()
    assert data["responsables"][0]["person"]["id"] == str(person.id)


@pytest.mark.asyncio
async def test_homonyms_are_left_unlinked(client, db_session, user_token, intllabus_source):
    """Linking to the wrong profile is worse than not linking at all."""
    course = await make_course(
        db_session, intllabus_source, external_id="1", teachers=[("RESPONSABLE", "Marie MARTIN")]
    )
    for _ in range(2):
        db_session.add(
            Person(id=uuid.uuid4(), kind="PROFESSOR", first_name="Marie", last_name="Martin")
        )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {user_token}"}
    data = (await client.get(f"/api/private/courses/{course.id}", headers=headers)).json()
    assert data["responsables"][0]["person"] is None


@pytest.mark.asyncio
async def test_person_profile_lists_the_courses_they_teach(
    client, db_session, user_token, intllabus_source, trombint_source
):
    from conftest import make_student

    person = await make_student(db_session, trombint_source, "Ada", "Lovelace", "alovelace")
    await make_course(
        db_session,
        intllabus_source,
        external_id="1",
        title="Réseaux avancés",
        teachers=[("RESPONSABLE", "Ada LOVELACE")],
    )
    await make_course(
        db_session, intllabus_source, external_id="2", title="Autre cours", teachers=[]
    )

    headers = {"Authorization": f"Bearer {user_token}"}
    data = (await client.get(f"/api/private/students/{person.id}", headers=headers)).json()

    assert [c["title"] for c in data["taught_courses"]] == ["Réseaux avancés"]
    assert data["taught_courses"][0]["role"] == "RESPONSABLE"


# ── Public catalog ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_public_catalog_is_readable_without_authentication(
    client, db_session, intllabus_source
):
    # Accent-free query: the SQLite test engine stubs `unaccent` as identity,
    # so accent folding is only exercised against real Postgres.
    await make_course(db_session, intllabus_source, external_id="1", title="Advanced Networks")

    listing = await client.get("/api/courses?q=advanced")
    assert listing.status_code == 200
    assert [c["title"] for c in listing.json()["courses"]] == ["Advanced Networks"]

    facets = await client.get("/api/courses/filters")
    assert facets.status_code == 200
    assert facets.json()["niveaux"] == ["M1"]


@pytest.mark.asyncio
async def test_public_course_sheet_never_exposes_person_links(
    client, db_session, intllabus_source
):
    course = await make_course(
        db_session, intllabus_source, external_id="1", teachers=[("RESPONSABLE", "Ada LOVELACE")]
    )
    db_session.add(
        Person(id=uuid.uuid4(), kind="PROFESSOR", first_name="Ada", last_name="Lovelace")
    )
    await db_session.commit()

    data = (await client.get(f"/api/courses/{course.id}")).json()
    teacher = data["responsables"][0]

    # The name is published by the official catalog, the directory link is not.
    assert teacher["name"] == "Ada LOVELACE"
    assert "person" not in teacher
