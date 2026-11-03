"""followme sessions opened with a donor's stored CAS credentials."""

import asyncio
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from sqlalchemy import select
from tsprint import TSPrintClient
from tsprint.exceptions import LoginError

from core.auth import decrypt_secret
from db.models import UserCredential

CAS_PROVIDER = "CAS"

# Swapped in tests to avoid hitting followme.
client_factory = TSPrintClient

# One followme session at a time per donor: PaperCut's web-print wizard is
# stateful per session and concurrent quota checks must see each other.
_locks: dict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)


class PrintShareError(Exception):
    """User-facing error (plain French message) with the HTTP status to return."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def donor_lock(donor_id: uuid.UUID) -> asyncio.Lock:
    return _locks[donor_id]


async def has_credentials(db, user_id: uuid.UUID) -> bool:
    return (await _credential(db, user_id)) is not None


async def _credential(db, user_id):
    return (
        await db.execute(
            select(UserCredential).where(
                UserCredential.user_id == user_id, UserCredential.provider == CAS_PROVIDER
            )
        )
    ).scalars().first()


@asynccontextmanager
async def followme(db, donor_id: uuid.UUID):
    """Yields a logged-in TSPrINT client for the donor, logged out on exit."""
    credential = await _credential(db, donor_id)
    if not credential:
        raise PrintShareError("Aucun identifiant CAS n'est enregistré pour ce compte.", 409)
    client = client_factory(
        decrypt_secret(credential.encrypted_username),
        decrypt_secret(credential.encrypted_password),
    )
    try:
        try:
            await client.login()
        except LoginError as e:
            raise PrintShareError("Connexion à followme impossible avec les identifiants du donneur.", 502) from e
        except httpx.HTTPError as e:
            raise PrintShareError("followme ne répond pas. Réessayez dans quelques minutes.", 503) from e
        yield client
    finally:
        try:
            await client.logout()
        except Exception:
            pass
        await client.aclose()
