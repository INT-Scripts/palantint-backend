"""People seen from the campus map: whereabouts, past residents, linked homes, room presence.

These routes back the action row of a place in `/palantint/campus`. They answer
with people and the flats (`Location` APARTMENT) they currently live in; the map
places flats by `location_id`. Event times are Paris wall-clock, as in ADE.
"""

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.private.agenda import (
    event_for_person_clause,
    get_event_class_groups,
    room_label,
)
from api.private.deps import require_user
from api.private.indoor import place_identities
from db.database import get_db
from db.models import (
    Event,
    EventAttendee,
    Location,
    Organization,
    OrganizationMembership,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
)

router = APIRouter(
    prefix="/maps/indoor", tags=["indoor-maps"], dependencies=[Depends(require_user)]
)

PARIS = ZoneInfo("Europe/Paris")
# Cohorts shared on the map: a promo or a class group, not a whole school or programme.
COHORT_KINDS = ("PROMO", "CLASS_GROUP")
# Organizations whose members a local stands for: the club and its board.
MEMBER_KINDS = ("CLUB", "BUREAU")


def paris_now(at=None):
    return at or datetime.now(PARIS).replace(tzinfo=None)


def person_summary(person):
    return {"id": str(person.id), "first_name": person.first_name, "last_name": person.last_name}


def event_summary(event):
    return {
        "id": str(event.id),
        "name": event.name,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat(),
        "room": room_label(event.location),
    }


async def get_person(db, person_id):
    person = await db.get(Person, person_id)
    if not person or not person.is_active:
        raise HTTPException(status_code=404, detail="Personne introuvable")
    return person


async def current_homes(db, person_ids):
    """Current flat of each person, as {person_id: location_id}."""
    if not person_ids:
        return {}
    rows = (
        await db.execute(
            select(PersonHousing.person_id, PersonHousing.location_id)
            .join(Location, Location.id == PersonHousing.location_id)
            .where(
                PersonHousing.person_id.in_(person_ids),
                PersonHousing.ended_at.is_(None),
                Location.kind == "APARTMENT",
                Location.is_active.is_(True),
            )
        )
    ).all()
    return {row.person_id: row.location_id for row in rows}


async def homes_of(db, people):
    """Flats of the given people, grouped by flat: [{location_id, people: [{…, link}]}].

    ``people`` maps a person to the label of their link (relationship, role, group).
    People without a current flat are left out: the map has nowhere to put them.
    """
    homes = await current_homes(db, list(people))
    persons = {
        person.id: person
        for person in (
            await db.execute(select(Person).where(Person.id.in_(list(homes)), Person.is_active.is_(True)))
        ).scalars()
    }
    grouped = defaultdict(list)
    for person_id, location_id in homes.items():
        if person_id in persons:
            grouped[location_id].append({**person_summary(persons[person_id]), "link": people[person_id]})
    return [
        {"location_id": str(location_id), "people": sorted(entries, key=lambda entry: (entry["last_name"], entry["first_name"]))}
        for location_id, entries in grouped.items()
    ]


@router.get("/people/{person_id}/whereabouts")
async def whereabouts(person_id: uuid.UUID, at: datetime | None = None, db: AsyncSession = Depends(get_db)):
    """Where the person's timetable puts them now, and their next class with a room."""
    person = await get_person(db, person_id)
    now = paris_now(at)
    events = (
        (
            await db.execute(
                select(Event)
                .options(selectinload(Event.location))
                .where(
                    event_for_person_clause(person_id),
                    Event.location_id.is_not(None),
                    Event.end_time > now,
                    Event.start_time < now + timedelta(days=7),
                )
                .order_by(Event.start_time)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    current = next((event for event in events if event.start_time <= now), None)
    upcoming = next((event for event in events if event.start_time > now), None)
    return {
        "person": person_summary(person),
        "now": event_summary(current) if current else None,
        "next": event_summary(upcoming) if upcoming else None,
    }


@router.get("/locations/{location_id}/residents")
async def residents(location_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Everyone who lived in the place, most recent first. Synchronisations close stays instead of deleting them."""
    _, location_ids = await place_identities(db, location_id)
    rows = (
        await db.execute(
            select(PersonHousing, Person)
            .join(Person, Person.id == PersonHousing.person_id)
            .where(PersonHousing.location_id.in_(location_ids))
            .order_by(PersonHousing.ended_at.is_(None).desc(), PersonHousing.ended_at.desc(), PersonHousing.started_at.desc())
        )
    ).all()
    return {
        "residents": [
            {
                **person_summary(person),
                "started_at": stay.started_at.isoformat() if stay.started_at else None,
                "ended_at": stay.ended_at.isoformat() if stay.ended_at else None,
            }
            for stay, person in rows
        ]
    }


@router.get("/people/{person_id}/circle")
async def circle(person_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Homes of the people linked to the person: research relationships and roommates."""
    await get_person(db, person_id)
    rows = (
        await db.execute(
            select(PersonRelationship.person_a_id, PersonRelationship.person_b_id, RelationshipType.name)
            .join(RelationshipType, RelationshipType.id == PersonRelationship.relationship_type_id)
            .where(or_(PersonRelationship.person_a_id == person_id, PersonRelationship.person_b_id == person_id))
        )
    ).all()
    people = {}
    for row in rows:
        other = row.person_b_id if row.person_a_id == person_id else row.person_a_id
        if other != person_id:
            people.setdefault(other, row.name)
    home = (await current_homes(db, [person_id])).get(person_id)
    if home:
        for other in (
            await db.execute(
                select(PersonHousing.person_id).where(
                    PersonHousing.location_id == home,
                    PersonHousing.ended_at.is_(None),
                    PersonHousing.person_id != person_id,
                )
            )
        ).scalars():
            people.setdefault(other, "Coloc")
    return {"homes": await homes_of(db, people)}


@router.get("/people/{person_id}/cohort")
async def cohort(person_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Homes of the people sharing a promo or a class group with the person."""
    await get_person(db, person_id)
    groups = (
        await db.execute(
            select(Organization.id, Organization.name, Organization.kind)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(
                OrganizationMembership.person_id == person_id,
                OrganizationMembership.ended_at.is_(None),
                Organization.kind.in_(COHORT_KINDS),
                Organization.is_active.is_(True),
            )
        )
    ).all()
    # The class group is the closer link: it wins over the promo.
    groups = sorted(groups, key=lambda group: COHORT_KINDS.index(group.kind), reverse=True)
    people = {}
    for group in groups:
        for other in (
            await db.execute(
                select(OrganizationMembership.person_id).where(
                    OrganizationMembership.organization_id == group.id,
                    OrganizationMembership.ended_at.is_(None),
                    OrganizationMembership.person_id != person_id,
                )
            )
        ).scalars():
            people.setdefault(other, group.name)
    return {"groups": [group.name for group in groups], "homes": await homes_of(db, people)}


@router.get("/organizations/{organization_id}/homes")
async def organization_homes(organization_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Homes of an association's members, its board included; board roles are the link label."""
    organization = await db.get(Organization, organization_id)
    if not organization or not organization.is_active:
        raise HTTPException(status_code=404, detail="Association introuvable")
    boards = (
        await db.execute(select(Organization.id).where(Organization.parent_id == organization_id, Organization.kind == "BUREAU"))
    ).scalars().all()
    rows = (
        await db.execute(
            select(OrganizationMembership.person_id, OrganizationMembership.role, OrganizationMembership.is_mandat, OrganizationMembership.organization_id)
            .where(
                OrganizationMembership.organization_id.in_([organization_id, *boards]),
                OrganizationMembership.ended_at.is_(None),
            )
        )
    ).all()
    people, board = {}, set()
    for row in rows:
        on_board = row.is_mandat or row.organization_id in boards
        if on_board:
            board.add(row.person_id)
        if on_board or row.person_id not in people:
            people[row.person_id] = row.role or "Membre"
    homes = await homes_of(db, people)
    for home in homes:
        for entry in home["people"]:
            entry["board"] = uuid.UUID(entry["id"]) in board
    return {"organization": {"id": str(organization.id), "name": organization.name}, "board": len(board), "members": len(people), "homes": homes}


@router.get("/locations/{location_id}/presence")
async def presence(location_id: uuid.UUID, at: datetime | None = None, db: AsyncSession = Depends(get_db)):
    """Who is in the room: the class happening now (else the last one of the day), its groups and students, and where they live."""
    _, location_ids = await place_identities(db, location_id)
    now = paris_now(at)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = (
        (
            await db.execute(
                select(Event)
                .options(selectinload(Event.location), selectinload(Event.organization))
                .where(
                    Event.location_id.in_(location_ids),
                    Event.is_active.is_(True),
                    Event.start_time <= now,
                    Event.end_time > day,
                )
                .order_by(Event.start_time.desc())
            )
        )
        .scalars()
        .all()
    )
    event = next((item for item in events if item.end_time > now), None) or next(iter(events), None)
    if not event:
        return {"event": None, "ongoing": False, "groups": [], "people": 0, "homes": []}
    groups = await get_event_class_groups(db, event)
    attendees = (await db.execute(select(EventAttendee.person_id).where(EventAttendee.event_id == event.id))).scalars().all()
    people = {person: "Inscrit" for person in attendees}
    # As in the timetables: listed attendees when there are some, otherwise the class groups.
    if not attendees and groups:
        for row in (
            await db.execute(
                select(OrganizationMembership.person_id, Organization.name)
                .join(Organization, Organization.id == OrganizationMembership.organization_id)
                .where(
                    and_(
                        OrganizationMembership.organization_id.in_([group.id for group in groups]),
                        OrganizationMembership.ended_at.is_(None),
                    )
                )
            )
        ).all():
            people.setdefault(row.person_id, row.name)
    return {
        "event": event_summary(event),
        "ongoing": event.end_time > now,
        "groups": [{"id": str(group.id), "name": group.name} for group in groups],
        "people": len(people),
        "homes": await homes_of(db, people),
    }


@router.get("/locations/{location_id}/groups")
async def week_groups(location_id: uuid.UUID, day: date | None = None, db: AsyncSession = Depends(get_db)):
    """Class groups using the room during the week of ``day`` (default: this week), with their number of sessions."""
    _, location_ids = await place_identities(db, location_id)
    monday = datetime.combine((day or paris_now().date()), datetime.min.time())
    monday -= timedelta(days=monday.weekday())
    events = (
        (
            await db.execute(
                select(Event)
                .options(selectinload(Event.organization))
                .where(
                    Event.location_id.in_(location_ids),
                    Event.is_active.is_(True),
                    Event.start_time >= monday,
                    Event.start_time < monday + timedelta(days=7),
                )
            )
        )
        .scalars()
        .all()
    )
    sessions = defaultdict(int)
    names = {}
    for event in events:
        for group in await get_event_class_groups(db, event):
            sessions[group.id] += 1
            names[group.id] = group.name
    return {
        "week": monday.date().isoformat(),
        "events": len(events),
        "groups": sorted(
            ({"id": str(group), "name": names[group], "sessions": count} for group, count in sessions.items()),
            key=lambda item: (-item["sessions"], item["name"]),
        ),
    }
