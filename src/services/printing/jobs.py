"""Print request lifecycle on a donor's followme account.

PENDING_APPROVAL → APPROVED → UPLOADING → HELD → RELEASED → RECONCILED,
or CANCELLED / REJECTED / EXPIRED / FAILED. Only the followme job whose name
is the request's `queued_name` is ever released or cancelled: the donor's own
jobs are never touched.
"""

import asyncio
import io
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from sqlalchemy import select

from core.config import settings as app_settings
from db.models import (
    Location,
    Notification,
    PrintGrant,
    PrintRequest,
    PrintShareSettings,
    User,
    utc_now,
)
from services.printing import quota
from services.printing.session import PrintShareError, donor_lock, followme

# followme rates (pages per printed page), used if the rates page can't be read.
FALLBACK_RATES = {"grayscale": 1, "color": 8}
QUEUE_CACHE_SECONDS = 3600
FIND_JOB_ATTEMPTS = 10
FIND_JOB_DELAY = 2.0


@dataclass
class Queue:
    index: int
    name: str
    rate: int  # worst-case cost of one printed page


_queue_cache: dict = {"at": 0.0, "queues": None}


# ── PDF ───────────────────────────────────────────────────────────────────────

def parse_page_range(spec: Optional[str], page_count: int) -> list[int]:
    """'1-3,5' → [0, 1, 2, 4] (zero-based, in order, without duplicates)."""
    if not spec or not spec.strip():
        return list(range(page_count))
    pages: list[int] = []
    for part in spec.replace(" ", "").split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise PrintShareError(f"Plage de pages invalide : « {part} ». Exemple : 1-3,5", 422)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > page_count:
            raise PrintShareError(f"Plage de pages hors du document ({page_count} page(s)) : « {part} ».", 422)
        pages.extend(p - 1 for p in range(start, end + 1) if p - 1 not in pages)
    return pages


def prepare_pdf(data: bytes, page_range: Optional[str], dest: Path) -> int:
    """Validates the PDF, applies the page range and writes it to dest. Returns the printed page count."""
    if len(data) > app_settings.PRINT_MAX_UPLOAD_MB * 1024 * 1024:
        raise PrintShareError(f"Le fichier dépasse {app_settings.PRINT_MAX_UPLOAD_MB} Mo.", 413)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PrintShareError("Les PDF protégés par mot de passe ne sont pas acceptés.", 422)
        page_count = len(reader.pages)
    except PdfReadError as e:
        raise PrintShareError("Le fichier n'est pas un PDF valide.", 422) from e
    if page_count == 0:
        raise PrintShareError("Le PDF ne contient aucune page.", 422)
    pages = parse_page_range(page_range, page_count)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(pages) == page_count:
        dest.write_bytes(data)
    else:
        writer = PdfWriter()
        for index in pages:
            writer.add_page(reader.pages[index])
        with dest.open("wb") as f:
            writer.write(f)
    return len(pages)


def queued_name(request_id: uuid.UUID, filename: str) -> str:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:60] or "document"
    return f"palantint-{request_id.hex[:8]}-{stem}.pdf"


def _remove_temp(request: PrintRequest) -> None:
    if request.temp_path:
        Path(request.temp_path).unlink(missing_ok=True)
        request.temp_path = None


# ── followme helpers ──────────────────────────────────────────────────────────

async def web_queues(client) -> dict[str, Queue]:
    """Web-print queues by mode ('grayscale' / 'color') with their worst-case page rate."""
    cached = _queue_cache["queues"]
    if cached and time.monotonic() - _queue_cache["at"] < QUEUE_CACHE_SECONDS:
        return cached
    printers = await client.get_webprint_printers()
    try:
        rates = await client.get_rates()
    except Exception:
        rates = {}
    queues = {}
    for index, label in enumerate(printers):
        mode = "color" if re.search(r"colou?r", label, re.I) else "grayscale"
        if mode in queues:
            continue
        name = max((key for key in rates if label.startswith(key)), key=len, default=label)
        sizes = rates.get(name) or {}
        size = sizes.get("A4 (ISO_A4)") or next(iter(sizes.values()), {})
        rate = size.get(mode)
        queues[mode] = Queue(index, name, int(rate) if rate else FALLBACK_RATES[mode])
    if not queues:
        raise PrintShareError("Aucune file d'impression web n'est disponible sur followme.", 502)
    _queue_cache.update(at=time.monotonic(), queues=queues)
    return queues


async def read_balance(db, client, settings: PrintShareSettings) -> int:
    summary = await client.get_summary()
    if summary.get("balance_value") is None:
        raise PrintShareError("Impossible de lire le solde followme du donneur.", 502)
    settings.last_balance = int(summary["balance_value"])
    settings.balance_checked_at = utc_now()
    return settings.last_balance


async def find_job(client, name: str, attempts: int = 1):
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(FIND_JOB_DELAY)
        for job in await client.get_pending_jobs():
            if job["name"] == name:
                return job
    return None


def notify(db, user_id: uuid.UUID, kind: str, title: str, message: str, request: Optional[PrintRequest] = None):
    payload = {"print_request_id": str(request.id)} if request else {}
    db.add(Notification(user_id=user_id, kind=kind, title=title, message=message, payload=payload))


async def _username(db, user_id: uuid.UUID) -> str:
    user = await db.get(User, user_id)
    return user.username if user else "un membre"


# ── Lookups ───────────────────────────────────────────────────────────────────

async def get_settings(db, donor_id: uuid.UUID, *, for_update: bool = False) -> Optional[PrintShareSettings]:
    stmt = select(PrintShareSettings).where(PrintShareSettings.user_id == donor_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


async def get_request(db, request_id: uuid.UUID) -> PrintRequest:
    request = await db.get(PrintRequest, request_id)
    if not request:
        raise PrintShareError("Demande d'impression introuvable.", 404)
    return request


def _ensure_party(request: PrintRequest, user: User, *, donor: bool = True, requester: bool = True) -> None:
    allowed = user.is_admin or (donor and request.donor_user_id == user.id) or (requester and request.requester_user_id == user.id)
    if not allowed:
        raise PrintShareError("Cette demande d'impression ne vous concerne pas.", 403)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def submit(
    db,
    requester: User,
    donor_id: uuid.UUID,
    data: bytes,
    filename: str,
    *,
    copies: int = 1,
    color: bool = False,
    page_range: Optional[str] = None,
    use_pool: bool = False,
) -> PrintRequest:
    if donor_id == requester.id:
        raise PrintShareError("Vous ne pouvez pas utiliser votre propre partage.", 400)
    if not 1 <= copies <= 99:
        raise PrintShareError("Le nombre de copies doit être compris entre 1 et 99.", 422)
    if not filename.lower().endswith(".pdf"):
        raise PrintShareError("Seuls les fichiers PDF sont acceptés.", 415)

    request_id = uuid.uuid4()
    temp = app_settings.PRINT_UPLOAD_DIR / f"{request_id}.pdf"
    page_count = prepare_pdf(data, page_range, temp)
    committed = False
    try:
        async with donor_lock(donor_id):
            settings = quota.ensure_sharing(await get_settings(db, donor_id, for_update=True))
            grant = None if use_pool else await quota.active_grant(db, donor_id, requester.id)
            if not use_pool and not grant:
                raise PrintShareError("Ce membre ne vous a pas accordé de pages.", 403)
            async with followme(db, donor_id) as client:
                queue = (await web_queues(client)).get("color" if color else "grayscale")
                if not queue:
                    raise PrintShareError("Ce mode d'impression n'est pas disponible.", 422)
                estimate = page_count * copies * queue.rate
                balance = await read_balance(db, client, settings)
                await quota.check(db, settings, requester.id, grant, estimate, balance)
                now = utc_now()
                request = PrintRequest(
                    id=request_id,
                    requester_user_id=requester.id,
                    donor_user_id=donor_id,
                    grant_id=grant.id if grant else None,
                    status="APPROVED" if settings.auto_approve else "PENDING_APPROVAL",
                    original_filename=filename[:255],
                    queued_name=queued_name(request_id, filename),
                    page_count=page_count,
                    page_range=page_range or None,
                    copies=copies,
                    color=color,
                    estimated_cost=estimate,
                    temp_path=str(temp),
                    approval_expires_at=None if settings.auto_approve else now + timedelta(hours=app_settings.PRINT_APPROVAL_HOURS),
                )
                db.add(request)
                if request.status == "PENDING_APPROVAL":
                    notify(db, donor_id, "PRINT_REQUEST_PENDING", "Demande d'impression à valider",
                           f"{requester.username} souhaite imprimer « {filename} » ({estimate} page(s) de quota).", request)
                await db.commit()
                committed = True
                if request.status == "APPROVED":
                    await _upload(db, client, request, queue)
                return request
    except BaseException:
        if not committed:
            temp.unlink(missing_ok=True)
            # Nothing but the freshly read balance is pending: keep it and release the row lock.
            # (A rollback would expire the caller's objects, e.g. the current user.)
            try:
                await db.commit()
            except Exception:
                await db.rollback()
        raise


async def _upload(db, client, request: PrintRequest, queue: Queue) -> None:
    request.status = "UPLOADING"
    await db.commit()
    try:
        await client.upload_file(request.temp_path, copies=request.copies, printer_index=queue.index,
                                 filename=request.queued_name)
        job = await find_job(client, request.queued_name, attempts=FIND_JOB_ATTEMPTS)
        if not job:
            raise PrintShareError("Le document a été envoyé mais n'apparaît pas dans la file d'attente followme.", 502)
        request.status = "HELD"
        request.hold_expires_at = utc_now() + timedelta(minutes=app_settings.PRINT_HOLD_MINUTES)
    except Exception as e:
        request.status = "FAILED"
        request.error = e.message if isinstance(e, PrintShareError) else "L'envoi du document à followme a échoué."
        notify(db, request.requester_user_id, "PRINT_FAILED", "Impression impossible", request.error, request)
        raise PrintShareError(request.error, 502) from e
    finally:
        _remove_temp(request)
        await db.commit()


async def approve(db, donor: User, request_id: uuid.UUID) -> PrintRequest:
    request = await get_request(db, request_id)
    _ensure_party(request, donor, requester=False)
    if request.status != "PENDING_APPROVAL":
        raise PrintShareError("Cette demande n'attend plus de validation.", 409)
    async with donor_lock(request.donor_user_id):
        settings = await get_settings(db, request.donor_user_id, for_update=True)
        grant = await db.get(PrintGrant, request.grant_id) if request.grant_id else None
        async with followme(db, request.donor_user_id) as client:
            queue = (await web_queues(client))["color" if request.color else "grayscale"]
            balance = await read_balance(db, client, settings)
            await quota.check(db, settings, request.requester_user_id, grant, request.estimated_cost, balance,
                              exclude_id=request.id)
            request.status = "APPROVED"
            request.approval_expires_at = None
            notify(db, request.requester_user_id, "PRINT_REQUEST_DECIDED", "Demande acceptée",
                   f"« {request.original_filename} » a été acceptée et envoyée à followme.", request)
            await db.commit()
            await _upload(db, client, request, queue)
    return request


async def reject(db, donor: User, request_id: uuid.UUID) -> PrintRequest:
    request = await get_request(db, request_id)
    _ensure_party(request, donor, requester=False)
    if request.status != "PENDING_APPROVAL":
        raise PrintShareError("Cette demande n'attend plus de validation.", 409)
    request.status = "REJECTED"
    _remove_temp(request)
    notify(db, request.requester_user_id, "PRINT_REQUEST_DECIDED", "Demande refusée",
           f"{await _username(db, request.donor_user_id)} a refusé l'impression de « {request.original_filename} ».", request)
    await db.commit()
    return request


async def printers(db, user: User, request_id: uuid.UUID) -> list[dict]:
    request = await get_request(db, request_id)
    _ensure_party(request, user)
    if request.status != "HELD":
        raise PrintShareError("Le document n'est pas en attente de libération.", 409)
    async with donor_lock(request.donor_user_id), followme(db, request.donor_user_id) as client:
        job = await find_job(client, request.queued_name)
        if not job:
            raise PrintShareError("Le document n'est plus dans la file d'attente followme.", 410)
        found = await client.get_physical_printers(job)
    places = await printer_places(db)
    return [{"name": p["name"], "status": p["status"], "available": "OK" in p["status"],
             "feature_id": places[p["name"]].attributes["wemap"].get("primary_feature_id") if p["name"] in places else None,
             "place": places[p["name"]].name if p["name"] in places else None} for p in found]


async def printer_places(db) -> dict[str, Location]:
    """Mapped place of each followme printer, from the seeded print points."""
    locations = (await db.execute(select(Location).where(Location.is_active.is_(True)))).scalars()
    return {name: location for location in locations
            for name in (location.attributes or {}).get("printing", {}).get("printers", [])}


async def release(db, user: User, request_id: uuid.UUID, printer_name: str) -> PrintRequest:
    request = await get_request(db, request_id)
    _ensure_party(request, user, donor=False)
    if request.status != "HELD":
        raise PrintShareError("Le document n'est pas en attente de libération.", 409)
    quota.ensure_sharing(await get_settings(db, request.donor_user_id))
    async with donor_lock(request.donor_user_id), followme(db, request.donor_user_id) as client:
        job = await find_job(client, request.queued_name)
        if not job:
            raise PrintShareError("Le document n'est plus dans la file d'attente followme.", 410)
        available = [p for p in await client.get_physical_printers(job) if p["name"] == printer_name]
        if not available or "OK" not in available[0]["status"]:
            raise PrintShareError("Cette imprimante n'est pas disponible actuellement.", 409)
        await client.release_job(job, printer_name_filter=printer_name)
    request.status = "RELEASED"
    request.printer_name = printer_name
    request.released_at = utc_now()
    await db.commit()
    return request


async def _cancel_open(db, client, request: PrintRequest, status: str) -> None:
    """Cancels an open request; client is required when it is HELD (best effort if the job vanished)."""
    if request.status == "HELD" and client is not None:
        job = await find_job(client, request.queued_name)
        if job:
            await client.cancel_job(job)
    request.status = status
    _remove_temp(request)


async def cancel(db, user: User, request_id: uuid.UUID) -> PrintRequest:
    request = await get_request(db, request_id)
    _ensure_party(request, user)
    if request.status not in quota.OPEN:
        raise PrintShareError("Cette demande ne peut plus être annulée.", 409)
    if request.status == "HELD":
        async with donor_lock(request.donor_user_id), followme(db, request.donor_user_id) as client:
            await _cancel_open(db, client, request, "CANCELLED")
    else:
        await _cancel_open(db, None, request, "CANCELLED")
    if user.id != request.requester_user_id:
        notify(db, request.requester_user_id, "PRINT_REQUEST_DECIDED", "Impression annulée",
               f"L'impression de « {request.original_filename} » a été annulée.", request)
    await db.commit()
    return request


async def cancel_grant_requests(db, grant: PrintGrant) -> int:
    """Cancels every open request made through a grant (on revoke). Returns how many were cancelled."""
    open_requests = (
        await db.execute(select(PrintRequest).where(PrintRequest.grant_id == grant.id, PrintRequest.status.in_(quota.OPEN)))
    ).scalars().all()
    if not open_requests:
        return 0
    held = [r for r in open_requests if r.status == "HELD"]
    if held:
        try:
            async with donor_lock(grant.donor_user_id), followme(db, grant.donor_user_id) as client:
                for request in held:
                    await _cancel_open(db, client, request, "CANCELLED")
        except PrintShareError:
            # followme unreachable: the held jobs still expire on their own; never charge them.
            for request in held:
                await _cancel_open(db, None, request, "CANCELLED")
    for request in open_requests:
        if request.status != "CANCELLED":
            await _cancel_open(db, None, request, "CANCELLED")
        notify(db, request.requester_user_id, "PRINT_GRANT_CHANGED", "Autorisation retirée",
               f"L'impression de « {request.original_filename} » a été annulée car l'autorisation a été retirée.", request)
    return len(open_requests)


async def revoke_grant(db, actor: User, grant: PrintGrant) -> PrintGrant:
    if grant.revoked_at is None:
        grant.revoked_at = utc_now()
        grant.revoked_by_user_id = actor.id
        await cancel_grant_requests(db, grant)
        notify(db, grant.beneficiary_user_id, "PRINT_GRANT_CHANGED", "Autorisation d'impression retirée",
               f"{await _username(db, grant.donor_user_id)} a retiré les pages qui vous étaient accordées.")
        await db.commit()
    return grant
