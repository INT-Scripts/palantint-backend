"""MCP tool surface: the contract clients such as BananaBot rely on, and tool behaviour
against a stubbed private API (no database, no network)."""

import json

import httpx
import pytest
import pytest_asyncio
from fastmcp import Client

from palantint_mcp.server import GUIDE, mcp
from palantint_mcp.server import client as api

EXPECTED_ORDER = [
    "get_student",
    "search_directory",
    "get_student_schedule",
    "where_is_student",
    "find_common_free_slots",
    "get_club",
    "get_class_roster",
    "find_shortest_path",
    "get_room_schedule",
    "find_available_rooms",
    "get_apartment_info",
    "get_laundry_status",
    "list_reference",
]

ENZO = {"id": "u-enzo", "trombint_id": "emartin", "first_name": "Enzo", "last_name": "Martin"}
BASILE = {"id": "u-basile", "trombint_id": None, "first_name": "Basile", "last_name": "MARTIN"}
CLAIRE = {"id": "u-claire", "trombint_id": None, "first_name": "Claire", "last_name": "Martin"}


@pytest_asyncio.fixture
async def stub_api():
    """Route the client's HTTP calls to `routes`: {(method, path): payload | callable}."""
    routes = {}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/private")
        calls.append((request.method, path, dict(request.url.params)))
        route = routes.get((request.method, path))
        if route is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        if callable(route):
            body = json.loads(request.content) if request.content else None
            route = route(dict(request.url.params), body)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    original = api._http
    api._http = httpx.AsyncClient(
        base_url="http://test/api/private", transport=httpx.MockTransport(handler)
    )
    api._cache.clear()
    try:
        yield routes, calls
    finally:
        await api._http.aclose()
        api._http = original
        api._cache.clear()


async def run(tool, **args):
    async with Client(mcp) as c:
        result = await c.call_tool(tool, args, raise_on_error=False)
    return result.is_error, "\n".join(getattr(part, "text", "") for part in result.content)


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tools_are_listed_in_deliberate_order_with_read_only_annotations():
    async with Client(mcp) as c:
        tools = await c.list_tools()
    assert [t.name for t in tools] == EXPECTED_ORDER
    for tool in tools:
        assert tool.title, tool.name
        assert tool.annotations and tool.annotations.readOnlyHint is True, tool.name
        assert tool.outputSchema is None, tool.name


@pytest.mark.asyncio
async def test_descriptions_say_what_and_when_and_every_parameter_is_described():
    async with Client(mcp) as c:
        tools = await c.list_tools()
    for tool in tools:
        # Parameters are documented once, in the schema — not again in prose.
        assert "Args:" not in tool.description and "\n" not in tool.description.strip(), tool.name
        assert len(tool.description) <= 160, tool.name
        for name, prop in tool.inputSchema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name}"


@pytest.mark.asyncio
async def test_server_guidance_fits_client_note_budget():
    # BananaBot appends a server's prompts to every tool description, capped at 400.
    async with Client(mcp) as c:
        prompt = await c.get_prompt("tool_guide")
        assert c.initialize_result.instructions == GUIDE
    assert prompt.messages[0].content.text == GUIDE
    assert len(GUIDE) <= 300


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_ambiguous_name_is_an_answer_listing_candidates(stub_api):
    routes, _ = stub_api
    routes[("GET", "/search")] = {"students": [ENZO, BASILE, CLAIRE]}

    is_error, text = await run("get_student", student="Martin")

    assert not is_error
    assert "emartin Enzo Martin" in text and "Basile MARTIN" in text
    assert "Retry with one TrombintID or exact full name" in text


@pytest.mark.asyncio
async def test_exact_full_name_resolves_a_student_without_trombint_id(stub_api):
    routes, _ = stub_api
    routes[("GET", "/search")] = {"students": [ENZO, BASILE, CLAIRE]}
    routes[("GET", "/students/u-basile")] = {"promo": "Ingénieur 1ère année", "apartment": "7413"}

    is_error, text = await run("get_student", student="basile martin")

    assert not is_error
    assert text.splitlines()[0].startswith("Basile MARTIN")
    assert "apt: 7413" in text


@pytest.mark.asyncio
async def test_bad_date_is_an_error_showing_the_format_before_any_api_call(stub_api):
    _, calls = stub_api

    is_error, text = await run("get_student_schedule", student="emartin", date="28/07")

    assert is_error
    assert "YYYY-MM-DD" in text and "28/07" in text
    assert calls == []


@pytest.mark.asyncio
async def test_server_failure_tells_the_model_not_to_retry(stub_api):
    routes, _ = stub_api
    routes[("GET", "/search")] = httpx.Response(503)

    is_error, text = await run("get_student", student="emartin")

    assert is_error
    assert "Do not retry" in text


@pytest.mark.asyncio
async def test_rejected_arguments_carry_the_api_detail(stub_api):
    routes, _ = stub_api
    routes[("GET", "/agenda/rooms/available")] = httpx.Response(
        400, json={"detail": "End time must be after start time"}
    )

    is_error, text = await run(
        "find_available_rooms", start_time="2026-11-03T14:00", end_time="2026-11-03T15:00"
    )

    assert is_error
    assert "End time must be after start time" in text


@pytest.mark.asyncio
async def test_class_roster_prefers_exact_name_and_pages_with_a_next_call(stub_api):
    routes, _ = stub_api
    routes[("GET", "/class-groups")] = [{"id": "g10", "name": "INF10"}, {"id": "g1", "name": "INF1"}]
    members = [{"first_name": f"S{i:02d}", "last_name": f"L{i:02d}"} for i in range(70)]
    routes[("GET", "/class-groups/g1")] = {"name": "INF1", "members": members}

    _, first = await run("get_class_roster", group="inf1")
    _, second = await run("get_class_roster", group="inf1", offset=60)

    assert first.startswith("INF1 (shown 1-60 of 70):")
    assert "(more: call get_class_roster(group='INF1', offset=60) or narrow the query)" in first
    assert second.startswith("INF1 (shown 61-70 of 70):")
    assert "S69 L69" in second and "(more" not in second


@pytest.mark.asyncio
async def test_schedule_shows_rooms(stub_api):
    routes, _ = stub_api
    routes[("GET", "/search")] = {"students": [ENZO]}
    routes[("POST", "/agenda/compare")] = lambda params, body: {
        "u-enzo": [
            {
                "name": "Réseaux",
                "type": "COURSE",
                "room": "J02",
                "start_time": f"{body['start_date']}T14:00:00",
                "end_time": f"{body['start_date']}T17:15:00",
            }
        ]
    }

    _, text = await run("get_student_schedule", student="emartin", date="2026-11-03")

    assert "14:00-17:15 Réseaux (COURSE) @ J02" in text


@pytest.mark.asyncio
async def test_common_free_slots_report_excluded_students(stub_api):
    routes, _ = stub_api
    routes[("GET", "/search")] = lambda params, body: (
        {"students": [ENZO]} if params["q"] == "emartin" else {"students": []}
    )
    routes[("POST", "/agenda/compare")] = {
        "u-enzo": [{"name": "Maths", "start_time": "2026-11-03T08:15:00", "end_time": "2026-11-03T11:30:00"}]
    }

    _, text = await run("find_common_free_slots", students=["emartin", "nobody"], date="2026-11-03")

    assert "excluded: No student matches 'nobody'" in text
    assert "08:00-08:15 (15m)" in text and "11:30-20:00 (510m)" in text


@pytest.mark.asyncio
async def test_shortest_path_names_each_link(stub_api):
    routes, _ = stub_api
    routes[("GET", "/search")] = lambda params, body: {
        "students": [ENZO if params["q"] == "emartin" else {**BASILE, "trombint_id": "bmartin"}]
    }
    routes[("GET", "/graph")] = {
        "nodes": [
            {"id": "u-enzo", "name": "Enzo Martin"},
            {"id": "u-mid", "name": "Mid Person"},
            {"id": "u-basile", "name": "Basile Martin"},
        ],
        "links": [
            {"source": "u-enzo", "target": "u-mid", "label": "friend"},
            {"source": "u-basile", "target": "u-mid", "label": "club MiNET"},
        ],
    }

    _, text = await run("find_shortest_path", from_student="emartin", to_student="bmartin")

    assert text == "2 degrees\nEnzo Martin -(friend)- Mid Person -(club MiNET)- Basile Martin"


# --------------------------------------------------------------------------- #
# Private API the schedule tools depend on
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_agenda_compare_returns_each_event_room(client, db_session, user_token):
    from datetime import datetime

    from db.models import Event, EventAttendee, Location, Person

    student = Person(first_name="Room", last_name="Check")
    room = Location(kind="ROOM", code="J02", name="J02 SC48pl")
    db_session.add_all([student, room])
    await db_session.flush()
    event = Event(
        name="Réseaux",
        location_id=room.id,
        start_time=datetime(2026, 11, 3, 14),
        end_time=datetime(2026, 11, 3, 17, 15),
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(EventAttendee(event_id=event.id, person_id=student.id))
    await db_session.commit()

    response = await client.post(
        "/api/private/agenda/compare",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"student_ids": [str(student.id)], "start_date": "2026-11-03", "end_date": "2026-11-03"},
    )

    assert response.status_code == 200
    assert response.json()[str(student.id)][0]["room"] == "J02 SC48pl"
