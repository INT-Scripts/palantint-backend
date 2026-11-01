"""Text formatting shared by every tool.

Responses are plain `key: value` text rather than Markdown, because bold markers and
headings cost tokens without telling the model anything. Every list is capped, and a
capped list always says so and names the exact call that continues it: a model shown
half a list with no marker answers confidently about the half it can't see.
"""

from datetime import datetime
from typing import Any, Dict, List, Tuple

MAX_ROWS = 40


def fields(*pairs: Tuple[str, Any]) -> str:
    """Join `key: value` pairs on one line, dropping the ones with no value.

    Omitting a field entirely is cheaper than printing 'N/A' and reads the same.
    """
    return ", ".join(f"{k}: {v}" for k, v in pairs if v)


def who(person: Dict[str, Any]) -> str:
    """'trombintid Firstname Lastname', falling back to the name alone."""
    name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
    tid = person.get("trombint_id")
    return f"{tid} {name}" if tid else name


def hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def clip(text: str, limit: int) -> str:
    """Truncate free text (descriptions, notes) and mark the cut."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…(truncated)"


def joined(label: str, items: List[str], limit: int = 10) -> str:
    """`label: a; b; c (+N more)` on one line — for short lists that don't page."""
    if not items:
        return ""
    extra = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return f"{label}: " + "; ".join(items[:limit]) + extra


def event_line(event: Dict[str, Any], with_date: bool = False) -> str:
    """`HH:MM-HH:MM name (type) @ room`, prefixed with the date when asked."""
    start = datetime.fromisoformat(event["start_time"])
    stamp = f"{start:%Y-%m-%d %H:%M}" if with_date else f"{start:%H:%M}"
    line = f"{stamp}-{hhmm(event['end_time'])} {event['name']}"
    if event.get("type"):
        line += f" ({event['type']})"
    if event.get("room"):
        line += f" @ {event['room']}"
    return line


def call(tool: str, **args: Any) -> str:
    """Render a tool call the model can copy verbatim: `tool(a='x', offset=40)`."""
    return f"{tool}(" + ", ".join(f"{k}={v!r}" for k, v in args.items() if v not in ("", None)) + ")"


def page(
    title: str,
    rows: List[str],
    offset: int = 0,
    limit: int = MAX_ROWS,
    more: str = "",
    has_more: bool | None = None,
) -> str:
    """Render one page of `rows` under `title`.

    `rows` is the full list, sliced here. When the API already paginated it, pass the
    page itself with `has_more` instead (its total is then unknown). `more` is the tool
    call, minus offset, that fetches the next page — see `call`.
    """
    if has_more is None:
        total: int | None = len(rows)
        rows = rows[offset : offset + limit]
        has_more = offset + len(rows) < total
    else:
        total = None
    if not rows:
        return f"{title}: none" if offset == 0 else f"{title}: nothing past offset {offset}"

    end = offset + len(rows)
    if offset == 0 and not has_more:
        header = f"{title} ({end}):"
    else:
        header = f"{title} (shown {offset + 1}-{end}" + (f" of {total}):" if total else "):")
    body = "\n".join(rows)
    if has_more:
        if more:
            sep = "" if more.endswith("()") else ", "
            body += f"\n(more: call {more[:-1]}{sep}offset={end}) or narrow the query)"
        else:
            body += "\n(more: narrow the query)"
    return f"{header}\n{body}"
