import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.routes import User, get_current_user
from db.database import get_db
from db.models import AgendaEvent, Student, StudentAgendaEvent, StudentClub

router = APIRouter(tags=["agenda"])



from typing import List
from pydantic import BaseModel

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

def get_implicit_calendar_ids(promo: str, ecole: str) -> List[str]:
    cids = []
    if not promo: return cids
    
    is_tsp = ecole and ("Télécom SudParis" in ecole or "TSP" in ecole)
    is_imt = ecole and ("Business School" in ecole or "IMT-BS" in ecole)
    
    if promo == "Ingénieur 1ère année":
        if is_tsp: cids.extend(["PRJ67059", "PRJ67368", "PRJ67367", "PRJ67371"])
        if is_imt: cids.extend(["PRJ68141", "PRJ68109"])
    elif promo == "Ingénieur 2ème année":
        if is_tsp: cids.extend(["PRJ67199", "PRJ67369", "PRJ67367", "PRJ67371"])
        if is_imt: cids.extend(["PRJ68173", "PRJ68235", "PRJ68225"])
    elif promo == "Ingénieur 3ème année":
        if is_tsp: cids.extend(["PRJ66964", "PRJ67372"])
        if is_imt: cids.extend(["PRJ68454", "PRJ68297", "PRJ68312", "PRJ68334", "PRJ68344", "PRJ68356", "PRJ68374", "PRJ68388", "PRJ68406", "PRJ68425", "PRJ68439"])
    elif promo == "Bachelor 1ère année":
        cids.append("PRJ67617")
    elif promo == "Bachelor 2ème année":
        cids.append("PRJ67654")
    elif promo == "Bachelor 3ème année":
        cids.extend(["PRJ68289", "PRJ68203"])
    elif promo == "Mastère Spécialisé":
        cids.extend(["PRJ68821", "PRJ68843"])
    elif promo in ["Master of Science", "Diplôme National de Master"]:
        cids.extend(["PRJ68041", "PRJ67817", "PRJ68085", "PRJ67924"])
        
    return cids

@router.post("/agenda/compare")
async def compare_agendas(
    req: CompareRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches agendas for multiple students and returns them grouped by student."""
    try:
        dt_start = datetime.strptime(req.start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(req.end_date, "%Y-%m-%d")
        
        results = {}

        for student_id in req.student_ids:
            student = await db.get(Student, student_id)
            if not student: continue
            
            promo_cals = get_implicit_calendar_ids(student.promo, student.ecole)

            club_ids_subq = select(StudentClub.club_id).where(
                StudentClub.student_id == student_id
            )

            stmt = (
                select(AgendaEvent)
                .outerjoin(
                    StudentAgendaEvent, StudentAgendaEvent.event_id == AgendaEvent.id
                )
                .options(selectinload(AgendaEvent.club))
                .where(
                    or_(
                        StudentAgendaEvent.student_id == student_id,
                        AgendaEvent.club_id.in_(club_ids_subq),
                        AgendaEvent.calendar_id.in_(promo_cals) if promo_cals else False,
                    ),
                    AgendaEvent.start_time >= dt_start,
                    AgendaEvent.end_time <= dt_end,
                )
                .order_by(AgendaEvent.start_time)
            )

            result = await db.execute(stmt)
            events = {e.id: e for e in result.scalars().all()}.values()
            
            results[str(student_id)] = [
                {
                    "id": str(e.id),
                    "name": e.name,
                    "start_time": e.start_time.isoformat(),
                    "end_time": e.end_time.isoformat(),
                    "type": e.type,
                    "club_name": e.club.name if e.club else None,
                }
                for e in events
            ]

        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/agenda/rooms/list")
async def get_all_rooms(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns a unique list of all rooms found in the agenda database."""
    try:
        stmt = select(AgendaEvent.room).distinct().where(AgendaEvent.room != None).order_by(AgendaEvent.room)
        result = await db.execute(stmt)
        rooms = [r for r in result.scalars().all() if r]
        return sorted(list(set(rooms)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/agenda/rooms/available")
async def get_available_rooms(
    start_time: str, # ISO format
    end_time: str,   # ISO format
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns a list of rooms that have no events during the specified period."""
    try:
        dt_start = parse_iso_datetime(start_time)
        dt_end = parse_iso_datetime(end_time)

        # Subquery for occupied rooms during this period
        occupied_stmt = select(AgendaEvent.room).where(
            or_(
                and_(AgendaEvent.start_time <= dt_start, AgendaEvent.end_time > dt_start),
                and_(AgendaEvent.start_time < dt_end, AgendaEvent.end_time >= dt_end),
                and_(AgendaEvent.start_time >= dt_start, AgendaEvent.end_time <= dt_end)
            )
        ).distinct()
        
        result = await db.execute(occupied_stmt)
        occupied_rooms = set(r for r in result.scalars().all() if r)

        # Get all rooms
        all_rooms_res = await db.execute(select(AgendaEvent.room).distinct().where(AgendaEvent.room != None))
        all_rooms = set(r for r in all_rooms_res.scalars().all() if r)

        available = sorted(list(all_rooms - occupied_rooms))
        return available
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/agenda/rooms/occupancy")
async def get_room_occupancy(
    room_query: str,
    start_date: str,  # YYYY-MM-DD
    end_date: str,    # YYYY-MM-DD
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Checks occupancy for rooms matching the query string within a time range."""
    try:
        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

        stmt = select(AgendaEvent).where(
            AgendaEvent.room.ilike(f"%{room_query}%"),
            AgendaEvent.start_time >= dt_start,
            AgendaEvent.start_time < dt_end
        ).order_by(AgendaEvent.start_time)

        result = await db.execute(stmt)
        events = result.scalars().all()

        return [
            {
                "id": str(e.id),
                "name": e.name,
                "room": e.room,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "type": e.type
            }
            for e in events
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/students/{student_id}/agenda")
async def get_student_agenda(
    student_id: uuid.UUID,
    start_date: str = None,  # format YYYY-MM-DD
    end_date: str = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Student, student_id)
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

        # Subquery for student's clubs
        club_ids_subq = select(StudentClub.club_id).where(
            StudentClub.student_id == student_id
        )

        promo_cals = get_implicit_calendar_ids(student.promo, student.ecole)

        # Query events from db.database: direct course links OR club links
        stmt = (
            select(AgendaEvent)
            .outerjoin(
                StudentAgendaEvent, StudentAgendaEvent.event_id == AgendaEvent.id
            )
            .options(selectinload(AgendaEvent.club))
            .where(
                or_(
                    StudentAgendaEvent.student_id == student_id,
                    AgendaEvent.club_id.in_(club_ids_subq),
                    AgendaEvent.calendar_id.in_(promo_cals) if promo_cals else False,
                ),
                AgendaEvent.start_time >= dt_start,
                AgendaEvent.end_time <= dt_end,
            )
            .order_by(AgendaEvent.start_time)
        )

        result = await db.execute(stmt)
        # Deduplicate using set to avoid same event twice if it somehow was manually linked + club linked
        events = {e.id: e for e in result.scalars().all()}.values()

        return [
            {
                "id": str(e.id),
                "name": e.name,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "room": e.room,
                "description": e.description,
                "professors": e.professors,
                "type": e.type,
                "club_id": str(e.club.id) if e.club else None,
                "club_name": e.club.name if e.club else None,
                "club_color": e.club.color_primary if e.club else None,
            }
            for e in events
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch agenda: {str(e)}")


@router.get("/agenda/events/{event_id}")
async def get_agenda_event_details(
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(AgendaEvent)
        .options(
            selectinload(AgendaEvent.students).selectinload(StudentAgendaEvent.student),
            selectinload(AgendaEvent.club),
        )
        .where(AgendaEvent.id == event_id)
    )
    result = await db.execute(stmt)
    event = result.scalars().first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    students = []
    for sae in event.students:
        s = sae.student
        students.append(
            {
                "id": str(s.id),
                "first_name": s.first_name,
                "last_name": s.last_name,
                "promo": s.promo,
                "profile_picture_path": s.profile_picture_path,
            }
        )

    return {
        "id": str(event.id),
        "name": event.name,
        "type": event.type,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat(),
        "room": event.room,
        "description": event.description,
        "professors": event.professors,
        "club_id": str(event.club.id) if event.club else None,
        "club_name": event.club.name if event.club else None,
        "club_color": event.club.color_primary if event.club else None,
        "students": sorted(
            students, key=lambda x: (x["last_name"] or "", x["first_name"] or "")
        ),
    }
