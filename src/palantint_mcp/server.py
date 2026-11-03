import logging
import secrets
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from mcp.types import ToolAnnotations

from core.config import settings
from core.rate_limit import get_client_ip, mcp_auth_limiter

from .client import client
from .tools import clubs, directory, housing, printing, schedule

logger = logging.getLogger("mcp")

MCP_SERVICE_TOKEN = settings.MCP_SERVICE_TOKEN

# How to work with these tools, once, for every tool. Clients such as BananaBot fold a
# server's prompts into each tool description and cap them at 400 characters, so this
# stays short and says nothing a single tool's own description already says.
GUIDE = (
    "Student tools take a TrombintID or full name directly; never invent an ID. "
    "An ambiguous name returns candidate IDs: retry with one. "
    "Dates are YYYY-MM-DD, times YYYY-MM-DDTHH:MM, Paris time. "
    "A '(more: call …)' line means the list was cut: make that call or narrow the query."
)


class ServiceTokenVerifier(TokenVerifier):
    """Grants access to MCP clients that present MCP_SERVICE_TOKEN as their bearer token.

    This is the same shared secret the MCP server uses to authenticate itself to the
    private API (see api/private/deps.py) — it does not carry per-client identity, it
    simply gates access to the /mcp endpoint itself, which is otherwise unauthenticated.

    Failed verifications are throttled via the same core.rate_limit machinery used by
    every other credential-checking endpoint in the app (login, refresh), keyed by client IP.
    Successful ones are not counted: every JSON-RPC message (each tool call,
    list_tools, notifications…) is a separate POST that re-presents the token, so a bot
    such as BananaBot would otherwise lock itself out mid-conversation.
    """

    def __init__(self, expected_token: str):
        super().__init__()
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        try:
            client_ip: Optional[str] = get_client_ip(get_http_request())
        except RuntimeError:
            # No HTTP request in context (e.g. non-HTTP transport) — nothing to key on.
            client_ip = None

        if client_ip and mcp_auth_limiter.is_limited(client_ip):
            # verify_token runs inside Starlette's AuthenticationMiddleware, outside
            # FastAPI's exception-handling layer, so a raised HTTPException would
            # surface as a raw 500 rather than 429. Deny instead: this reuses the
            # same 401 "invalid_token" response an ordinary bad token gets, so a
            # rate-limited caller learns nothing beyond "not authenticated".
            logger.warning("MCP auth rate limit exceeded, denying token verification.")
            return None

        if not secrets.compare_digest(token, self._expected_token):
            if client_ip:
                mcp_auth_limiter.record(client_ip)
            return None
        return AccessToken(token=token, client_id="mcp-service", scopes=[])


if not MCP_SERVICE_TOKEN:
    logger.warning(
        "MCP_SERVICE_TOKEN is not set — the /mcp endpoint will run WITHOUT authentication."
    )

mcp = FastMCP(
    "PalantINT",
    instructions=GUIDE,
    auth=ServiceTokenVerifier(MCP_SERVICE_TOKEN) if MCP_SERVICE_TOKEN else None,
)


@mcp.prompt()
def tool_guide() -> str:
    """How to use the PalantINT tools."""
    return GUIDE


# Every tool only reads. Most read PalantINT's own database; the laundry reads a live
# external service, so it is open-world and gets more time.
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
LIVE = ToolAnnotations(readOnlyHint=True, idempotentHint=False, openWorldHint=True)
TIMEOUT = 15.0

# Order is deliberate: it is the order the schemas reach the model, and a model that
# isn't reasoning about the choice tends to take the first tool that plausibly fits.
# Questions about a person are the common case, so those tools come first.
TOOLS = [
    (directory.get_student, "Student profile", {}),
    (directory.search_directory, "Search the directory", {}),
    (schedule.get_student_schedule, "Student timetable", {}),
    (schedule.where_is_student, "Where a student is", {}),
    (schedule.find_common_free_slots, "Common free time", {}),
    (clubs.get_club, "Club details and members", {}),
    (clubs.get_class_roster, "Class roster", {}),
    (directory.find_shortest_path, "Connection between two students", {}),
    (schedule.get_room_schedule, "Room timetable", {}),
    (schedule.find_available_rooms, "Free rooms", {}),
    (housing.get_apartment_info, "Apartment specs", {}),
    (housing.get_laundry_status, "Laundry availability", {"annotations": LIVE, "timeout": 20.0}),
    (printing.get_print_sharing_stats, "Print sharing totals", {}),
    (housing.list_reference, "Reference values", {}),
]

for fn, title, options in TOOLS:
    mcp.tool(
        fn,
        title=title,
        output_schema=None,
        **{"annotations": READ_ONLY, "timeout": TIMEOUT, **options},
    )

__all__ = ["mcp", "client"]
