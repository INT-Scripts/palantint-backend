"""Quota rules. Usage is always derived from PrintRequest rows, never stored."""

import uuid
from typing import Optional

from sqlalchemy import case, func, select

from db.models import PrintGrant, PrintRequest, PrintShareSettings, utc_now
from services.printing.session import PrintShareError

# Reserved in the donor's account but not yet debited by followme.
IN_FLIGHT = ("PENDING_APPROVAL", "APPROVED", "UPLOADING", "HELD")
# Counted against caps: in flight, released (estimate until reconciled), reconciled (actual cost).
COUNTED = IN_FLIGHT + ("RELEASED", "RECONCILED")
OPEN = IN_FLIGHT  # requests that can still be cancelled


def _cost():
    return func.coalesce(
        func.sum(
            case(
                (PrintRequest.status == "RECONCILED", func.coalesce(PrintRequest.actual_cost, PrintRequest.estimated_cost)),
                else_=PrintRequest.estimated_cost,
            )
        ),
        0,
    )


async def consumed(
    db,
    donor_id: uuid.UUID,
    *,
    grant_id: Optional[uuid.UUID] = None,
    pool_requester_id: Optional[uuid.UUID] = None,
    statuses=COUNTED,
    exclude_id: Optional[uuid.UUID] = None,
) -> int:
    stmt = select(_cost()).where(PrintRequest.donor_user_id == donor_id, PrintRequest.status.in_(statuses))
    if grant_id:
        stmt = stmt.where(PrintRequest.grant_id == grant_id)
    if pool_requester_id:
        stmt = stmt.where(PrintRequest.grant_id.is_(None), PrintRequest.requester_user_id == pool_requester_id)
    if exclude_id:
        stmt = stmt.where(PrintRequest.id != exclude_id)
    return int((await db.execute(stmt)).scalar_one())


def grant_is_active(grant: PrintGrant) -> bool:
    return grant.revoked_at is None and (grant.expires_at is None or grant.expires_at > utc_now())


async def active_grant(db, donor_id: uuid.UUID, beneficiary_id: uuid.UUID) -> Optional[PrintGrant]:
    grant = (
        await db.execute(
            select(PrintGrant).where(
                PrintGrant.donor_user_id == donor_id,
                PrintGrant.beneficiary_user_id == beneficiary_id,
                PrintGrant.revoked_at.is_(None),
            )
        )
    ).scalars().first()
    return grant if grant and grant_is_active(grant) else None


async def remaining(
    db,
    settings: PrintShareSettings,
    requester_id: uuid.UUID,
    grant: Optional[PrintGrant],
    *,
    balance: Optional[int] = None,
    exclude_id: Optional[uuid.UUID] = None,
) -> int:
    """Pages the requester can still use through this grant (or the pool when grant is None)."""
    donor_id = settings.user_id
    limits = []
    if grant:
        limits.append(grant.amount - await consumed(db, donor_id, grant_id=grant.id, exclude_id=exclude_id))
    elif settings.pool_per_person_cap is not None:
        limits.append(
            settings.pool_per_person_cap
            - await consumed(db, donor_id, pool_requester_id=requester_id, exclude_id=exclude_id)
        )
    if settings.global_cap is not None:
        limits.append(settings.global_cap - await consumed(db, donor_id, exclude_id=exclude_id))
    balance = settings.last_balance if balance is None else balance
    if balance is not None:
        pending = await consumed(db, donor_id, statuses=IN_FLIGHT, exclude_id=exclude_id)
        limits.append(balance - pending - settings.min_balance_floor)
    return max(0, min(limits)) if limits else 0


def ensure_sharing(settings: Optional[PrintShareSettings]) -> PrintShareSettings:
    if not settings or not settings.is_sharing:
        raise PrintShareError("Ce membre ne partage pas son quota d'impression.", 403)
    if settings.is_paused:
        raise PrintShareError("Le partage de ce membre est suspendu.", 403)
    return settings


async def check(
    db,
    settings: PrintShareSettings,
    requester_id: uuid.UUID,
    grant: Optional[PrintGrant],
    estimate: int,
    balance: int,
    exclude_id: Optional[uuid.UUID] = None,
) -> None:
    """Raises PrintShareError unless the grant/pool cap, the global cap and the balance floor all hold."""
    ensure_sharing(settings)
    donor_id = settings.user_id
    if grant:
        if not grant_is_active(grant):
            raise PrintShareError("Cette autorisation a été retirée ou a expiré.", 403)
        left = grant.amount - await consumed(db, donor_id, grant_id=grant.id, exclude_id=exclude_id)
        if estimate > left:
            raise PrintShareError(f"Autorisation insuffisante : {left} page(s) restante(s), {estimate} nécessaire(s).", 409)
    else:
        if not settings.pool_enabled:
            raise PrintShareError("Ce membre ne partage pas son quota avec tous les membres.", 403)
        if settings.pool_per_person_cap is not None:
            used = await consumed(db, donor_id, pool_requester_id=requester_id, exclude_id=exclude_id)
            left = settings.pool_per_person_cap - used
            if estimate > left:
                raise PrintShareError(f"Limite par personne atteinte : {left} page(s) restante(s), {estimate} nécessaire(s).", 409)
    if settings.global_cap is not None:
        left = settings.global_cap - await consumed(db, donor_id, exclude_id=exclude_id)
        if estimate > left:
            raise PrintShareError(f"Le plafond de partage du donneur est atteint ({left} page(s) restante(s)).", 409)
    pending = await consumed(db, donor_id, statuses=IN_FLIGHT, exclude_id=exclude_id)
    if balance - pending - estimate < settings.min_balance_floor:
        raise PrintShareError("Le solde du donneur ne permet pas cette impression.", 409)
