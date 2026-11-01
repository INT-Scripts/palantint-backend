"""People: profiles, directory search, and the relationship graph."""

import asyncio
from collections import deque
from typing import Annotated, Any, Dict, List, Literal, Tuple

from pydantic import Field

from ..client import GRAPH_TTL, client
from ..format import call, clip, fields, joined, page, who
from ..inputs import Offset, StudentRef, clamp

StudentInfo = Annotated[
    Literal["profile", "socials", "relations", "notes", "all"],
    Field(
        description="profile (promo, school, email, apartment, clubs, groups), socials, "
        "relations (friends, partners, roommates), notes (quotes left by others), or all. Default profile."
    ),
]


async def get_student(student: StudentRef, info: StudentInfo = "profile") -> str:
    """Look up one student. Use for any question about a specific person; a name works directly, no search needed first."""
    student_id, name = await client.resolve_student(student)
    if not student_id:
        return name

    # /students/{id} already carries socials, clubs, groups and notes, so only the
    # relations view costs a second request.
    want_rels = info in ("relations", "all")
    fetches = [client.get(f"/students/{student_id}")]
    if want_rels:
        fetches.append(client.get(f"/students/{student_id}/relationships"))
    results = await asyncio.gather(*fetches)
    data, rels = results[0], (results[1] if want_rels else [])

    tid = data.get("trombint_id")
    out = [f"{name} ({tid})" if tid else name]

    if info in ("profile", "all"):
        out.append(
            fields(
                ("promo", data.get("promo")),
                ("school", data.get("ecole")),
                ("email", data.get("email")),
                ("apt", data.get("apartment")),
            )
        )
        out.append(
            joined(
                "clubs",
                [f"{c.get('club_name')} ({c.get('role') or 'member'})" for c in data.get("clubs") or []],
                20,
            )
        )
        out.append(joined("groups", [g.get("class_group_name") for g in data.get("class_groups") or []], 20))

    if info in ("socials", "all"):
        socials = [f"{s['platform']} {s['url']}" for s in data.get("social_links") or []]
        out.append(joined("socials", socials, 20) or "socials: none")

    if want_rels:
        # Group by relationship type: one line per type beats one line per person.
        by_type: Dict[str, List[str]] = {}
        for rel in rels or []:
            by_type.setdefault(rel["relationship_type"]["name"].lower(), []).append(who(rel["other_student"]))
        out.append(
            page("relations", [joined(t, people, 15) for t, people in by_type.items()])
            if by_type
            else "relations: none"
        )

    if info in ("notes", "all"):
        notes = [m for m in (data.get("media") or []) if m.get("content")]
        out.append(
            page("notes", [f'"{clip(n["content"], 300)}" — {n.get("author_name") or "anonymous"}' for n in notes], limit=15)
            if notes
            else "notes: none"
        )

    return "\n".join(line for line in out if line)


async def search_directory(
    query: Annotated[
        str,
        Field(description="Name, TrombintID or apartment code; with filters, narrows by name, promo or school. Min 2 characters."),
    ] = "",
    promo: Annotated[str, Field(description="Exact promotion, e.g. 'Ingénieur 1ère année'; see list_reference(kind='promos').")] = "",
    school: Annotated[str, Field(description="Exact school: 'Télécom SudParis' or 'IMT-BS'.")] = "",
    building: Annotated[str, Field(description="Residence building filter, e.g. '7' for U7.")] = "",
    limit: Annotated[int, Field(description="Students per page, 1 to 50. Default 25.")] = 25,
    offset: Offset = 0,
) -> str:
    """Find students, clubs and class groups by name, or browse students by promo, school or building. Not needed before student tools: they take names."""
    limit = clamp(limit, 1, 50)
    offset = clamp(offset, 0, 10_000)
    more = call("search_directory", query=query, promo=promo, school=school, building=building, limit=limit)

    if promo or school or building:
        params: Dict[str, Any] = {"skip": offset, "limit": limit + 1}
        for key, value in (("q", query), ("promo", promo), ("ecole", school), ("bldg", building)):
            if value:
                params[key] = value

        students = await client.get("/students", params=params)
        if not students and offset == 0:
            filters = fields(("promo", promo), ("school", school), ("building", building), ("query", query))
            return f"No students match {filters}. Check the values with list_reference, or drop a filter."

        # Columns pinned by a filter are identical on every row, so print them once
        # in the header instead of repeating them on every line.
        pinned = fields(("promo", promo), ("school", school), ("building", building))
        rows = [
            ", ".join(
                [who(s)]
                + [
                    f"{k}: {v}"
                    for k, v in (
                        ("promo", None if promo else s.get("promo")),
                        ("school", None if school else s.get("ecole")),
                        ("apt", s.get("apartment")),
                    )
                    if v
                ]
            )
            for s in students[:limit]
        ]
        title = f"students {pinned}" if pinned else "students"
        return page(title, rows, offset, limit, more, has_more=len(students) > limit)

    if len(query.strip()) < 2:
        return "Give a query of at least 2 characters, or one of promo/school/building to browse."

    data = await client.get("/search", params={"q": query})
    out = []
    students = data.get("students", [])
    if students:
        out.append(
            page(
                "students",
                [f"{who(s)}, apt: {s['apartment']}" if s.get("apartment") else who(s) for s in students],
                offset,
                limit,
                more,
            )
        )
    out.append(joined("clubs", [f"{c['name']} ({c['slug']})" if c.get("slug") else c["name"] for c in data.get("clubs") or []]))
    out.append(joined("groups", [cg["name"] for cg in data.get("class_groups") or []]))
    out = [line for line in out if line]
    return "\n".join(out) if out else f"No results for '{query}'. Try part of the name, or browse with a promo/school/building filter."


async def find_shortest_path(from_student: StudentRef, to_student: StudentRef) -> str:
    """Shortest chain of relationships, roommates or shared clubs linking two students."""
    (sid1, who1), (sid2, who2) = await asyncio.gather(
        client.resolve_student(from_student), client.resolve_student(to_student)
    )
    if not sid1:
        return who1
    if not sid2:
        return who2
    if sid1 == sid2:
        return "Both refer to the same student."

    graph = await client.cached_get("/graph", GRAPH_TTL)
    names = {n["id"]: n.get("name") or n["id"] for n in graph.get("nodes", [])}

    adj: Dict[str, List[Tuple[str, str]]] = {}
    for link in graph.get("links", []):
        src, tgt, label = link["source"], link["target"], link.get("label") or "linked"
        adj.setdefault(src, []).append((tgt, label))
        adj.setdefault(tgt, []).append((src, label))

    # Parent pointers rather than a copied path per queued node.
    parent: Dict[str, Tuple[str, str]] = {sid1: (sid1, "")}
    queue = deque([sid1])
    while queue and sid2 not in parent:
        curr = queue.popleft()
        for neighbor, label in adj.get(curr, []):
            if neighbor not in parent:
                parent[neighbor] = (curr, label)
                queue.append(neighbor)

    if sid2 not in parent:
        return f"No path between {who1} and {who2}: they share no recorded relationship, room or club chain."

    hops: List[Tuple[str, str]] = []
    node = sid2
    while node != sid1:
        prev, label = parent[node]
        hops.append((label, node))
        node = prev

    # One line for the whole chain: "A -(friend)- B -(club BDE)- C".
    chain = names.get(sid1, who1)
    for label, node in reversed(hops):
        chain += f" -({label})- {names.get(node, node)}"
    return f"{len(hops)} degrees\n{chain}"
