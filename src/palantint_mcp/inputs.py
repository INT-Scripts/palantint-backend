"""Parameter types shared by the tools, and the validators behind them.

Parameter documentation lives here, in the schema, rather than in the tool
descriptions: the description says what a tool does and when to use it, and each
parameter is described once, next to its type. Limits stated in a description are
the limits clamped in code.

Times are Paris local time, naive, because that is how the timetable stores them.
"""

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastmcp.exceptions import ToolError
from pydantic import Field

PARIS = ZoneInfo("Europe/Paris")

StudentRef = Annotated[
    str, Field(description="TrombintID or full name, e.g. 'jdoe' or 'John Doe'.")
]
DateArg = Annotated[
    str, Field(description="YYYY-MM-DD, or today / tomorrow / yesterday. Default today.")
]
DateTimeArg = Annotated[
    str, Field(description="YYYY-MM-DDTHH:MM in Paris time, e.g. '2026-11-03T14:00'.")
]
Offset = Annotated[
    int, Field(description="Rows to skip, as given by a '(more: …)' line. Default 0.")
]


def clamp(value: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low


def now() -> datetime:
    return datetime.now(PARIS).replace(tzinfo=None)


def parse_date(value: str, name: str = "date") -> str:
    """Return `value` as YYYY-MM-DD, defaulting to today, or raise a ToolError
    that shows the expected shape."""
    raw = (value or "").strip().lower()
    relative = {"": 0, "today": 0, "tomorrow": 1, "yesterday": -1}
    if raw in relative:
        return (now().date() + timedelta(days=relative[raw])).isoformat()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ToolError(f"{name} must be YYYY-MM-DD (or today/tomorrow), got '{value}'.")


def parse_datetime(value: str, name: str = "datetime") -> datetime:
    """Parse an ISO datetime into naive Paris time, defaulting to now."""
    raw = (value or "").strip()
    if not raw or raw.lower() == "now":
        return now()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"{name} must be YYYY-MM-DDTHH:MM, got '{value}'.")
    if parsed.tzinfo:
        parsed = parsed.astimezone(PARIS).replace(tzinfo=None)
    return parsed
