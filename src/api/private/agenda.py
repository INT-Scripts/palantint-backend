import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.private.deps import User, escape_like, require_user
from db.database import get_db
from db.models import (
    Event,
    EventAttendee,
    EventOrganization,
    EventPresenter,
    Location,
    Organization,
    OrganizationMembership,
    Person,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agenda"])


class CompareRequest(BaseModel):
    student_ids: List[uuid.UUID]
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD


def parse_iso_datetime(iso_str: str) -> datetime:
    """Parses ISO datetime and ensures it is naive for DB comparison."""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.replace(tzinfo=None)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid ISO datetime format: {iso_str}")


def room_label(location: Optional[Location]) -> Optional[str]:
    """Flattens a resolved Location back into the single free-text room string
    the frontend expects (old AgendaEvent.room)."""
    if not location:
        return None
    return location.name or location.code


def student_class_group_ids_subquery(person_id_col):
    """Active CLASS_GROUP organization ids for a person (replaces
    StudentClassGroup.class_group_id)."""
    return (
        select(OrganizationMembership.organization_id)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.person_id == person_id_col,
            OrganizationMembership.ended_at.is_(None),
            Organization.kind == "CLASS_GROUP",
        )
    )


def event_in_class_groups_clause(class_group_ids_subq):
    """An event is "in" a set of class groups if its primary organization is
    one of them, or it's linked via the secondary EventOrganization table
    (old EventClassGroup was a plain m2m; the new schema splits primary vs.
    secondary organization but we union both to preserve old read behavior)."""
    secondary_match = (
        select(EventOrganization.event_id)
        .where(EventOrganization.organization_id.in_(class_group_ids_subq))
    )
    return or_(
        Event.organization_id.in_(class_group_ids_subq),
        Event.id.in_(secondary_match),
    )


async def get_event_professors(db: AsyncSession, event: Event) -> Optional[str]:
    """Prefers resolved EventPresenter names, falls back to presenters_raw."""
    result = await db.execute(
        select(Person)
        .join(EventPresenter, EventPresenter.person_id == Person.id)
        .where(EventPresenter.event_id == event.id)
    )
    presenters = result.scalars().all()
    if presenters:
        return ", ".join(
            f"{p.first_name} {p.last_name}".strip() for p in presenters
        )
    return event.presenters_raw


def event_club(event: Event) -> Optional[Organization]:
    """Old AgendaEvent.club: the event's primary organization when it's a club."""
    if event.organization and event.organization.kind in ("CLUB", "BUREAU"):
        return event.organization
    return None


@router.post("/agenda/compare")
async def compare_agendas(
    req: CompareRequest,
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches agendas for multiple students and returns them grouped by student."""
    try:
        dt_start = datetime.strptime(req.start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(req.end_date, "%Y-%m-%d")

        results = {}

        for student_id in req.student_ids:
            student = await db.get(Person, student_id)
            if not student:
                continue

            class_group_ids_subq = student_class_group_ids_subquery(student_id)

            stmt = (
                select(Event)
                .options(selectinload(Event.organization))
                .where(
                    event_in_class_groups_clause(class_group_ids_subq),
                    Event.start_time >= dt_start,
                    Event.end_time <= dt_end,
                )
                .order_by(Event.start_time)
            )

            result = await db.execute(stmt)
            events = {e.id: e for e in result.scalars().all()}.values()

            results[str(student_id)] = [
                {
                    "id": str(e.id),
                    "name": e.name,
                    "start_time": e.start_time.isoformat(),
                    "end_time": e.end_time.isoformat(),
                    "type": e.kind,
                    "club_name": event_club(e).name if event_club(e) else None,
                }
                for e in events
            ]

        return results
    except Exception:
        logger.exception("compare_agendas failed")
        raise HTTPException(status_code=500, detail="Failed to compare agendas")


@router.get("/agenda/rooms/list")
async def get_all_rooms(
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns a unique list of all rooms found in the agenda database."""
    try:
        stmt = (
            select(Location)
            .join(Event, Event.location_id == Location.id)
            .distinct()
        )
        result = await db.execute(stmt)
        rooms = [room_label(loc) for loc in result.scalars().all()]
        return sorted(set(r for r in rooms if r))
    except Exception:
        logger.exception("get_all_rooms failed")
        raise HTTPException(status_code=500, detail="Failed to fetch rooms")


@router.get("/agenda/rooms/available")
async def get_available_rooms(
    start_time: str, # ISO format
    end_time: str,   # ISO format
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns a list of rooms that have no events during the specified period."""
    try:
        dt_start = parse_iso_datetime(start_time)
        dt_end = parse_iso_datetime(end_time)

        # Subquery for occupied rooms during this period
        occupied_stmt = (
            select(Location)
            .join(Event, Event.location_id == Location.id)
            .where(
                or_(
                    and_(Event.start_time <= dt_start, Event.end_time > dt_start),
                    and_(Event.start_time < dt_end, Event.end_time >= dt_end),
                    and_(Event.start_time >= dt_start, Event.end_time <= dt_end)
                )
            )
            .distinct()
        )

        result = await db.execute(occupied_stmt)
        occupied_rooms = set(r for r in (room_label(loc) for loc in result.scalars().all()) if r)

        # Get all rooms
        all_rooms_stmt = select(Location).join(Event, Event.location_id == Location.id).distinct()
        all_rooms_res = await db.execute(all_rooms_stmt)
        all_rooms = set(r for r in (room_label(loc) for loc in all_rooms_res.scalars().all()) if r)

        available = sorted(all_rooms - occupied_rooms)
        return available
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.exception("get_available_rooms failed")
        raise HTTPException(status_code=500, detail="Failed to check room availability")


@router.get("/agenda/rooms/occupancy")
async def get_room_occupancy(
    room_query: str,
    start_date: str,  # YYYY-MM-DD
    end_date: str,    # YYYY-MM-DD
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db)
):
    """Checks occupancy for rooms matching the query string within a time range."""
    try:
        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

        escaped = escape_like(room_query)
        stmt = (
            select(Event)
            .options(selectinload(Event.location))
            .join(Location, Event.location_id == Location.id)
            .where(
                or_(
                    Location.code.ilike(f"%{escaped}%"),
                    Location.name.ilike(f"%{escaped}%"),
                ),
                Event.start_time >= dt_start,
                Event.start_time < dt_end
            )
            .order_by(Event.start_time)
        )

        result = await db.execute(stmt)
        events = result.scalars().all()

        return [
            {
                "id": str(e.id),
                "name": e.name,
                "room": room_label(e.location),
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "type": e.kind
            }
            for e in events
        ]
    except Exception:
        logger.exception("get_room_occupancy failed")
        raise HTTPException(status_code=500, detail="Failed to check room occupancy")


@router.get("/students/{student_id}/agenda")
async def get_student_agenda(
    student_id: uuid.UUID,
    start_date: str = None,  # format YYYY-MM-DD
    end_date: str = None,
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Person, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    try:
        if not start_date:
            dt_start = datetime.today().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            dt_start = datetime.strptime(start_date, "%Y-%m-%d")

        if not end_date:
            dt_end = dt_start + timedelta(days=7)
        else:
            dt_end = datetime.strptime(end_date, "%Y-%m-%d")

        class_group_ids_subq = student_class_group_ids_subquery(student_id)

        stmt = (
            select(Event)
            .options(
                selectinload(Event.organization),
                selectinload(Event.location),
            )
            .where(
                event_in_class_groups_clause(class_group_ids_subq),
                Event.start_time >= dt_start,
                Event.end_time <= dt_end,
            )
            .order_by(Event.start_time)
        )

        result = await db.execute(stmt)
        # Deduplicate using dict to avoid same event twice if it somehow was manually linked + club linked
        events = {e.id: e for e in result.scalars().all()}.values()

        response = []
        for e in events:
            club = event_club(e)
            class_group_names = await get_event_class_group_names(db, e)
            response.append(
                {
                    "id": str(e.id),
                    "name": e.name,
                    "start_time": e.start_time.isoformat(),
                    "end_time": e.end_time.isoformat(),
                    "room": room_label(e.location),
                    "description": e.description,
                    "professors": await get_event_professors(db, e),
                    "type": e.kind,
                    "club_id": str(club.id) if club else None,
                    "club_name": club.name if club else None,
                    "club_color": club.color_primary if club else None,
                    "class_groups": class_group_names,
                }
            )
        return response

    except Exception:
        logger.exception("get_student_agenda failed")
        raise HTTPException(status_code=500, detail="Failed to fetch agenda")


async def get_event_class_groups(db: AsyncSession, event: Event) -> List[Organization]:
    """CLASS_GROUP organizations linked to an event: its primary organization
    (if it is a class group) plus any secondary links via EventOrganization."""
    org_ids = set()
    if event.organization_id and event.organization and event.organization.kind == "CLASS_GROUP":
        org_ids.add(event.organization_id)

    result = await db.execute(
        select(EventOrganization.organization_id).where(EventOrganization.event_id == event.id)
    )
    org_ids.update(result.scalars().all())

    if not org_ids:
        return []

    result = await db.execute(
        select(Organization).where(Organization.id.in_(org_ids), Organization.kind == "CLASS_GROUP")
    )
    return result.scalars().all()


async def get_event_class_group_names(db: AsyncSession, event: Event) -> List[str]:
    groups = await get_event_class_groups(db, event)
    return [g.name for g in groups]


@router.get("/agenda/events/{event_id}")
async def get_agenda_event_details(
    event_id: uuid.UUID,
    current_user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Event)
        .options(
            selectinload(Event.organization),
            selectinload(Event.location),
        )
        .where(Event.id == event_id)
    )
    result = await db.execute(stmt)
    event = result.scalars().first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    class_groups = await get_event_class_groups(db, event)
    group_ids = [g.id for g in class_groups]

    # Get students from class groups (active memberships)
    group_students = []
    if group_ids:
        group_students_stmt = (
            select(Person)
            .join(OrganizationMembership, OrganizationMembership.person_id == Person.id)
            .where(
                OrganizationMembership.organization_id.in_(group_ids),
                OrganizationMembership.ended_at.is_(None),
            )
        )
        group_students_result = await db.execute(group_students_stmt)
        group_students = group_students_result.scalars().all()

    # Get manually linked students (attendees)
    manual_students_stmt = (
        select(Person)
        .join(EventAttendee, EventAttendee.person_id == Person.id)
        .where(EventAttendee.event_id == event_id)
    )
    manual_students_result = await db.execute(manual_students_stmt)
    manual_students = manual_students_result.scalars().all()

    # Combine and deduplicate
    all_students_dict = {s.id: s for s in (list(group_students) + list(manual_students))}

    students_data = []
    for s in all_students_dict.values():
        students_data.append(
            {
                "id": str(s.id),
                "first_name": s.first_name,
                "last_name": s.last_name,
                "profile_picture_path": s.profile_picture_path,
            }
        )

    club = event_club(event)

    return {
        "id": str(event.id),
        "name": event.name,
        "type": event.kind,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat(),
        "room": room_label(event.location),
        "description": event.description,
        "professors": await get_event_professors(db, event),
        "club_id": str(club.id) if club else None,
        "club_name": club.name if club else None,
        "club_color": club.color_primary if club else None,
        "class_groups": [g.name for g in class_groups],
        "students": sorted(
            students_data, key=lambda x: (x["last_name"] or "", x["first_name"] or "")
        ),
    }
