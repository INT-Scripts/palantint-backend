"""Background upkeep of print requests: expiry, warnings, reconciliation, temp cleanup."""

import asyncio
import time
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from core.config import settings as app_settings
from db.database import AsyncSessionLocal
from db.models import PrintRequest, utc_now
from services.printing.jobs import _cancel_open, _remove_temp, notify
from services.printing.session import PrintShareError, donor_lock, followme

# A released job missing from the print history after this delay keeps its estimate.
RECONCILE_GIVE_UP = timedelta(hours=24)


def _by_donor(requests):
    groups = defaultdict(list)
    for request in requests:
        groups[request.donor_user_id].append(request)
    return groups


async def _requests(db, *conditions):
    return (await db.execute(select(PrintRequest).where(*conditions))).scalars().all()


async def expire_approvals(db) -> None:
    for request in await _requests(db, PrintRequest.status == "PENDING_APPROVAL",
                                   PrintRequest.approval_expires_at < utc_now()):
        request.status = "EXPIRED"
        _remove_temp(request)
        notify(db, request.requester_user_id, "PRINT_REQUEST_DECIDED", "Demande expirée",
               f"« {request.original_filename} » n'a pas été validée à temps et a été annulée.", request)
    await db.commit()


async def warn_holds(db) -> None:
    soon = utc_now() + timedelta(minutes=app_settings.PRINT_HOLD_WARNING_MINUTES)
    for request in await _requests(db, PrintRequest.status == "HELD", PrintRequest.expiry_warned.is_(False),
                                   PrintRequest.hold_expires_at < soon):
        request.expiry_warned = True
        notify(db, request.requester_user_id, "PRINT_HOLD_EXPIRING", "Document bientôt annulé",
               f"« {request.original_filename} » sera annulé dans moins de "
               f"{app_settings.PRINT_HOLD_WARNING_MINUTES} minutes s'il n'est pas libéré.", request)
    await db.commit()


async def expire_holds(db) -> None:
    expired = await _requests(db, PrintRequest.status == "HELD", PrintRequest.hold_expires_at < utc_now())
    for donor_id, requests in _by_donor(expired).items():
        try:
            async with donor_lock(donor_id), followme(db, donor_id) as client:
                for request in requests:
                    await _cancel_open(db, client, request, "EXPIRED")
        except PrintShareError as e:
            print(f"Print share: could not cancel expired jobs for donor {donor_id}: {e.message}")
            continue  # retried on the next tick
        for request in requests:
            notify(db, request.requester_user_id, "PRINT_HOLD_EXPIRED", "Document annulé",
                   f"« {request.original_filename} » n'a pas été libéré dans l'heure et a été annulé.", request)
        await db.commit()


async def reconcile(db) -> None:
    released = await _requests(db, PrintRequest.status == "RELEASED")
    for donor_id, requests in _by_donor(released).items():
        try:
            async with donor_lock(donor_id), followme(db, donor_id) as client:
                logs = await client.get_print_logs(all_history=False)
        except PrintShareError as e:
            print(f"Print share: could not read print logs for donor {donor_id}: {e.message}")
            continue
        for request in requests:
            prefix = f"palantint-{request.id.hex[:8]}"
            costs = [log["cost_value"] for log in logs
                     if log.get("document", "").startswith(prefix) and log.get("cost_value") is not None]
            if costs:
                request.actual_cost = int(sum(costs))
                request.status = "RECONCILED"
            elif request.released_at and utc_now() - request.released_at > RECONCILE_GIVE_UP:
                request.actual_cost = request.estimated_cost
                request.status = "RECONCILED"
                request.error = "Coût réel introuvable dans l'historique followme : l'estimation est conservée."
        await db.commit()


def sweep_temp_files() -> None:
    """Removes PDFs left behind by a crash (requests keep them at most for the approval window)."""
    directory = app_settings.PRINT_UPLOAD_DIR
    if not directory.exists():
        return
    cutoff = time.time() - (app_settings.PRINT_APPROVAL_HOURS + 1) * 3600
    for path in directory.glob("*.pdf"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


async def run_once() -> None:
    async with AsyncSessionLocal() as db:
        await expire_approvals(db)
        await warn_holds(db)
        await expire_holds(db)
        await reconcile(db)
    sweep_temp_files()


async def print_share_loop():
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error in print share task: {e}")
        await asyncio.sleep(app_settings.PRINT_LOOP_SECONDS)
