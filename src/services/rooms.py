"""Room equivalence declared by persisted curation, shared by agenda consumers."""

from collections import defaultdict

from sqlalchemy import select

from core.places import normalized_name, room_alias
from db.models import Location


async def room_index(db):
    rows = (
        await db.execute(
            select(
                Location.id,
                Location.code,
                Location.name,
                Location.parent_id,
                Location.attributes["wemap"]["aliases"].label("aliases"),
                Location.attributes["wemap"]["active"].label("map_active"),
            ).where(Location.kind == "ROOM", Location.is_active.is_(True))
        )
    ).all()
    rooms = {row.id: row for row in rows}
    owners = defaultdict(set)
    for row in rows:
        if row.map_active is False:
            continue
        for alias in row.aliases or []:
            owners[normalized_name(alias)].add(row.id)
    canonical = {}
    for row in rows:
        candidates = (
            owners[normalized_name(row.name)] | owners[normalized_name(row.code)]
        )
        # An explicit parent on a different floor is evidence of a distinct
        # room; aliases must not silently cross that boundary.
        candidates = {
            identity
            for identity in candidates
            if row.parent_id is None
            or row.parent_id == rooms[identity].parent_id
            or identity == row.id
        }
        canonical[row.id] = next(iter(candidates)) if len(candidates) == 1 else row.id
    return rooms, canonical, owners


def canonical_labels(rows, index):
    rooms, canonical, _ = index
    return {
        rooms[canonical[row.id]].name or rooms[canonical[row.id]].code
        for row in rows
        if row.id in canonical
    }


def matching_rooms(query, index, capacity_alias=False, labels_only=False):
    rooms, canonical, owners = index
    normalize = room_alias if capacity_alias else normalized_name
    normalized = normalize(query)
    identities = {
        canonical[row.id]
        for row in rooms.values()
        if normalized
        in (
            {normalize(row.name or row.code)}
            if labels_only
            else {normalize(row.code), normalize(row.name)}
        )
    }
    declared = owners.get(normalized_name(query), set())
    return identities | declared
