"""Timetables: students' classes, common free time, and room occupancy."""

import asyncio
from datetime import datetime
from typing import Annotated, Any, Dict, List, Tuple

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..client import client
from ..format import call, event_line, page
from ..inputs import (
    DateArg,
    DateTimeArg,
    Offset,
    StudentRef,
    clamp,
    parse_date,
    parse_datetime,
)

DAY_START, DAY_END = 8 * 60, 20 * 60


async def _agenda(student_ids: List[str], day: str) -> Dict[str, Any]:
    return await client.post(
        "/agenda/compare",
        json={"student_ids": student_ids, "start_date": day, "end_date": day},
    )


def _minutes(iso: str, day: str) -> int:
    """Minutes since midnight of `day`, clamped to the day for events spanning midnight."""
    moment = datetime.fromisoformat(iso)
    if moment.date().isoformat() < day:
        return 0
    if moment.date().isoformat() > day:
        return 24 * 60
    return moment.hour * 60 + moment.minute


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def get_student_schedule(student: StudentRef, date: DateArg = "") -> str:
    """A student's classes on one day, with times and rooms."""
    day = parse_date(date)
    student_id, name = await client.resolve_student(student)
    if not student_id:
        return name

    events = (await _agenda([student_id], day)).get(str(student_id), [])
    if not events:
        return f"{name} has no class on {day}."
    return page(f"{name} {day}", [event_line(e) for e in events])


async def where_is_student(
    student: StudentRef,
    at: Annotated[str, Field(description="YYYY-MM-DDTHH:MM in Paris time. Default now.")] = "",
) -> str:
    """Infer where a student is at a moment: their classroom if they have class, else their apartment."""
    target = parse_datetime(at, "at")
    student_id, name = await client.resolve_student(student)
    if not student_id:
        return name

    data, agenda = await asyncio.gather(
        client.get(f"/students/{student_id}"),
        _agenda([student_id], target.date().isoformat()),
    )

    stamp = f"{target:%Y-%m-%d %H:%M}"
    for e in agenda.get(str(student_id), []):
        if datetime.fromisoformat(e["start_time"]) <= target <= datetime.fromisoformat(e["end_time"]):
            room = e.get("room") or "an unrecorded room"
            return f"{name} at {stamp}: in class {e['name']} ({e.get('type') or 'course'}) in {room}"

    apt = data.get("apartment")
    place = f"apartment {apt} (Maisel)" if apt else "unknown (no apartment on file)"
    return f"{name} at {stamp}: no class, likely at {place}"


async def find_common_free_slots(
    students: Annotated[List[str], Field(description="TrombintIDs or full names, 1 to 10.")],
    date: DateArg = "",
) -> str:
    """Time slots between 08:00 and 20:00 when every listed student is free of classes."""
    if not students:
        raise ToolError("students must list at least one TrombintID or name.")
    students = students[:10]
    day = parse_date(date)
    resolved = await asyncio.gather(*(client.resolve_student(s) for s in students))

    student_ids = [sid for sid, _ in resolved if sid]
    names = [name for sid, name in resolved if sid]
    unresolved = [message for sid, message in resolved if not sid]
    if not student_ids:
        return "\n".join(unresolved)

    agenda = await _agenda(student_ids, day)

    busy: List[Tuple[int, int]] = []
    for sid in student_ids:
        for e in agenda.get(str(sid), []):
            start = max(DAY_START, _minutes(e["start_time"], day))
            end = min(DAY_END, _minutes(e["end_time"], day))
            if start < end:
                busy.append((start, end))

    free: List[Tuple[int, int]] = []
    cursor = DAY_START
    for start, end in sorted(busy):
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < DAY_END:
        free.append((cursor, DAY_END))

    out = [f"free {day} for {', '.join(names)}:"]
    # Ambiguous or unknown names are reported with their candidates, and the slots
    # below exclude them — the model must not present them as covering everyone.
    out += [f"excluded: {message}" for message in unresolved]
    if free:
        out += [f"{_hm(s)}-{_hm(e)} ({e - s}m)" for s, e in free]
    else:
        out.append("no common slot between 08:00 and 20:00")
    return "\n".join(out)


async def get_room_schedule(
    room: Annotated[str, Field(description="Room name or part of it, e.g. 'B01' or 'amphi'.")],
    start_date: DateArg = "",
    end_date: Annotated[str, Field(description="YYYY-MM-DD, inclusive. Default start_date.")] = "",
    offset: Offset = 0,
) -> str:
    """Classes booked in rooms whose name contains `room`, over a range of days."""
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date") if end_date else start
    if end < start:
        raise ToolError(f"end_date {end} is before start_date {start}.")

    events = await client.get(
        "/agenda/rooms/occupancy",
        params={"room_query": room, "start_date": start, "end_date": end},
    )
    if not events:
        return (
            f"Nothing scheduled in rooms matching '{room}' from {start} to {end}. "
            "If the room name may be wrong, check list_reference(kind='rooms')."
        )
    span = start if start == end else f"{start} to {end}"
    return page(
        f"{room} {span}",
        [event_line(e, with_date=start != end) for e in events],
        clamp(offset, 0, 100_000),
        more=call("get_room_schedule", room=room, start_date=start, end_date=end),
    )


async def find_available_rooms(
    start_time: DateTimeArg,
    end_time: DateTimeArg,
    offset: Offset = 0,
) -> str:
    """Teaching rooms with no class scheduled during a time window."""
    start = parse_datetime(start_time, "start_time")
    end = parse_datetime(end_time, "end_time")
    if end <= start:
        raise ToolError("end_time must be after start_time.")

    window = (f"{start:%Y-%m-%dT%H:%M}", f"{end:%Y-%m-%dT%H:%M}")
    rooms = await client.get(
        "/agenda/rooms/available", params={"start_time": window[0], "end_time": window[1]}
    )
    if not rooms:
        return f"No room is free from {window[0]} to {window[1]}."
    return page(
        f"free {window[0]} to {end:%H:%M}" if start.date() == end.date() else f"free {window[0]} to {window[1]}",
        sorted(rooms),
        clamp(offset, 0, 100_000),
        60,
        call("find_available_rooms", start_time=window[0], end_time=window[1]),
    )
