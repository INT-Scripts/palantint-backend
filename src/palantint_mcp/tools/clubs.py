"""Organisations: clubs and class groups."""

from typing import Annotated, Literal

from pydantic import Field

from ..client import client
from ..format import call, clip, event_line, fields, joined, page, who
from ..inputs import Offset, clamp


def _by_name(person: dict) -> tuple:
    # Stable order, so that offsets page through the same list on every call.
    return ((person.get("last_name") or "").lower(), (person.get("first_name") or "").lower())


async def get_club(
    club: Annotated[str, Field(description="Club name or slug, e.g. 'BDE' or 'minet'.")],
    info: Annotated[
        Literal["details", "members"],
        Field(description="details (description, association, foyer room, links, upcoming events) or members (board, then members). Default details."),
    ] = "details",
    offset: Offset = 0,
) -> str:
    """A club or association: its details, or who runs it and who is in it."""
    data, note = await client.resolve_club(club)
    if not data:
        return note

    out = [f"{data['name']} ({data.get('slug') or '?'})", note]

    if info == "details":
        out.append(
            fields(
                ("type", data.get("type") or "club"),
                ("association", data.get("association_of_origin")),
                ("foyer", data.get("foyer_room")),
            )
        )
        if data.get("description"):
            out.append(clip(data["description"], 600))
        out.append(joined("links", [f"{ln['name']} {ln['url']}" for ln in data.get("links") or []]))
        if data.get("events"):
            out.append(page("events", [event_line(e, with_date=True) for e in data["events"]], limit=10))
        return "\n".join(line for line in out if line)

    members = data.get("members") or []
    if not members:
        return f"{data['name']}: no member roster on file."

    board = sorted((m for m in members if m.get("is_mandat")), key=_by_name)
    regular = sorted((m for m in members if not m.get("is_mandat")), key=_by_name)
    offset = clamp(offset, 0, 100_000)
    if offset == 0:
        out.append(joined("board", [f"{who(m)} ({m.get('role') or 'officer'})" for m in board], 20))
    if regular:
        out.append(
            page(
                "members",
                [who(m) for m in regular],
                offset,
                more=call("get_club", club=club, info="members"),
            )
        )
    return "\n".join(line for line in out if line)


async def get_class_roster(
    group: Annotated[str, Field(description="Class group name or part of it, e.g. 'Gp-EI2-TPG'.")],
    offset: Offset = 0,
) -> str:
    """Students in a class or promotion group."""
    matched, note = await client.resolve_group(group)
    if not matched:
        return note

    data = await client.get(f"/class-groups/{matched['id']}")
    members = sorted(data.get("members") or [], key=_by_name)
    if not members:
        return f"{data['name']}: no students."

    roster = page(
        data["name"],
        [who(m) for m in members],
        clamp(offset, 0, 100_000),
        60,
        call("get_class_roster", group=data["name"]),
    )
    return f"{note}\n{roster}" if note else roster
