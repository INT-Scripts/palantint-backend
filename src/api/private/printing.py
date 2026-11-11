"""followme print quota sharing: donor settings, grants, print requests and admin oversight."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.private.deps import User, require_admin, require_user
from db.database import get_db
from db.models import PrintGrant, PrintRequest, PrintShareSettings, utc_now
from services.printing import jobs, quota
from services.printing.session import (
    PrintShareError,
    donor_lock,
    followme,
    has_credentials,
)

router = APIRouter(prefix="/print", tags=["print"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class Member(BaseModel):
    id: uuid.UUID
    username: str


class SettingsIn(BaseModel):
    is_sharing: bool = False
    is_paused: bool = False
    pool_enabled: bool = False
    auto_approve: bool = True
    global_cap: Optional[int] = Field(default=None, ge=0)
    pool_per_person_cap: Optional[int] = Field(default=None, ge=0)
    min_balance_floor: int = Field(default=0, ge=0)


class SettingsOut(SettingsIn):
    has_credentials: bool
    last_balance: Optional[int] = None
    balance_checked_at: Optional[datetime] = None
    used_total: int  # pages consumed by others (reserved or printed)


class GrantIn(BaseModel):
    beneficiary_username: str
    amount: int = Field(ge=1, le=100000)
    expires_at: Optional[datetime] = None


class GrantPatch(BaseModel):
    amount: Optional[int] = Field(default=None, ge=1, le=100000)
    expires_at: Optional[datetime] = None


class GrantOut(BaseModel):
    id: uuid.UUID
    donor: Member
    beneficiary: Member
    amount: int
    used: int
    remaining: int
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]
    is_active: bool
    created_at: datetime


class DonorOut(BaseModel):
    donor: Member
    via: str  # GRANT | POOL
    grant_id: Optional[uuid.UUID] = None
    grant_expires_at: Optional[datetime] = None
    remaining: int
    auto_approve: bool


class RequestOut(BaseModel):
    id: uuid.UUID
    requester: Member
    donor: Member
    via: str
    status: str
    original_filename: str
    page_count: int
    page_range: Optional[str]
    copies: int
    color: bool
    printer_name: Optional[str]
    estimated_cost: int
    actual_cost: Optional[int]
    error: Optional[str]
    approval_expires_at: Optional[datetime]
    hold_expires_at: Optional[datetime]
    created_at: datetime
    released_at: Optional[datetime]


class PrinterOut(BaseModel):
    name: str
    status: str
    available: bool
    # Mapped place of the printer on /palantint/campus, when a print point names it.
    feature_id: Optional[str] = None
    place: Optional[str] = None


class ReleaseIn(BaseModel):
    printer_name: str


class PauseIn(BaseModel):
    is_paused: bool


class DonorOverview(BaseModel):
    donor: Member
    settings: SettingsOut
    active_grants: int


class StatsOut(BaseModel):
    sharing_donors: int
    active_grants: int
    requests_by_status: dict[str, int]
    pages_printed: int  # reconciled cost
    pages_reserved: int  # in flight or released, not yet reconciled


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _members(db, ids) -> dict[uuid.UUID, Member]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.username).where(User.id.in_(ids)))).all()
    return {row.id: Member(id=row.id, username=row.username) for row in rows}


def _unknown(user_id) -> Member:
    return Member(id=user_id, username="(compte supprimé)")


async def _settings_out(db, settings: PrintShareSettings) -> SettingsOut:
    return SettingsOut(
        **SettingsIn.model_validate(settings, from_attributes=True).model_dump(),
        has_credentials=await has_credentials(db, settings.user_id),
        last_balance=settings.last_balance,
        balance_checked_at=settings.balance_checked_at,
        used_total=await quota.consumed(db, settings.user_id),
    )


async def _own_settings(db, user: User) -> PrintShareSettings:
    settings = await jobs.get_settings(db, user.id)
    if not settings:
        settings = PrintShareSettings(user_id=user.id)
        db.add(settings)
        await db.flush()
    return settings


async def _grants_out(db, grants) -> list[GrantOut]:
    members = await _members(db, [g.donor_user_id for g in grants] + [g.beneficiary_user_id for g in grants])
    out = []
    for grant in grants:
        used = await quota.consumed(db, grant.donor_user_id, grant_id=grant.id)
        out.append(GrantOut(
            id=grant.id,
            donor=members.get(grant.donor_user_id) or _unknown(grant.donor_user_id),
            beneficiary=members.get(grant.beneficiary_user_id) or _unknown(grant.beneficiary_user_id),
            amount=grant.amount,
            used=used,
            remaining=max(0, grant.amount - used),
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            is_active=quota.grant_is_active(grant),
            created_at=grant.created_at,
        ))
    return out


async def _requests_out(db, requests) -> list[RequestOut]:
    members = await _members(db, [r.requester_user_id for r in requests] + [r.donor_user_id for r in requests])
    return [
        RequestOut(
            **{k: getattr(r, k) for k in RequestOut.model_fields if k not in ("requester", "donor", "via")},
            requester=members.get(r.requester_user_id) or _unknown(r.requester_user_id),
            donor=members.get(r.donor_user_id) or _unknown(r.donor_user_id),
            via="GRANT" if r.grant_id else "POOL",
        )
        for r in requests
    ]


async def _own_grant(db, user: User, grant_id: uuid.UUID) -> PrintGrant:
    grant = await db.get(PrintGrant, grant_id)
    if not grant or (grant.donor_user_id != user.id and not user.is_admin):
        raise PrintShareError("Autorisation introuvable.", 404)
    return grant


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    """Stored datetimes are naive UTC."""
    if value and value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# ── Donor ─────────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=SettingsOut)
async def get_share_settings(db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    settings = await _own_settings(db, user)
    await db.commit()
    return await _settings_out(db, settings)


@router.put("/settings", response_model=SettingsOut)
async def update_share_settings(body: SettingsIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    if body.is_sharing and not await has_credentials(db, user.id):
        raise PrintShareError("Enregistrez d'abord vos identifiants CAS dans votre compte pour partager votre quota.", 409)
    settings = await _own_settings(db, user)
    for key, value in body.model_dump().items():
        setattr(settings, key, value)
    await db.commit()
    return await _settings_out(db, settings)


@router.get("/balance", response_model=SettingsOut)
async def refresh_balance(db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    """Reads the live followme balance of the current user."""
    settings = await _own_settings(db, user)
    async with donor_lock(user.id), followme(db, user.id) as client:
        await jobs.read_balance(db, client, settings)
    await db.commit()
    return await _settings_out(db, settings)


@router.get("/members", response_model=list[Member])
async def search_members(q: str = Query(min_length=1, max_length=64), db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_user)):
    """PalantINT accounts matching a username, to grant pages to."""
    rows = (
        await db.execute(
            select(User.id, User.username)
            .where(func.lower(User.username).contains(q.lower()), User.id != user.id)
            .order_by(User.username)
            .limit(10)
        )
    ).all()
    return [Member(id=r.id, username=r.username) for r in rows]


@router.get("/grants", response_model=list[GrantOut])
async def list_grants(include_inactive: bool = False, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    stmt = select(PrintGrant).where(PrintGrant.donor_user_id == user.id).order_by(PrintGrant.created_at.desc())
    if not include_inactive:
        stmt = stmt.where(PrintGrant.revoked_at.is_(None))
    return await _grants_out(db, (await db.execute(stmt)).scalars().all())


@router.post("/grants", response_model=GrantOut, status_code=201)
async def create_grant(body: GrantIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    beneficiary = (await db.execute(select(User).where(User.username == body.beneficiary_username))).scalars().first()
    if not beneficiary:
        raise PrintShareError("Aucun compte PalantINT ne porte cet identifiant.", 404)
    if beneficiary.id == user.id:
        raise PrintShareError("Vous ne pouvez pas vous accorder des pages.", 400)
    existing = (
        await db.execute(select(PrintGrant).where(
            PrintGrant.donor_user_id == user.id,
            PrintGrant.beneficiary_user_id == beneficiary.id,
            PrintGrant.revoked_at.is_(None),
        ))
    ).scalars().first()
    if existing and quota.grant_is_active(existing):
        raise PrintShareError("Ce membre a déjà une autorisation active : modifiez-la plutôt.", 409)
    if existing:  # expired: close it so the new grant starts from zero
        existing.revoked_at = utc_now()
        existing.revoked_by_user_id = user.id
        await db.flush()
    grant = PrintGrant(donor_user_id=user.id, beneficiary_user_id=beneficiary.id, amount=body.amount,
                       expires_at=_naive(body.expires_at))
    db.add(grant)
    jobs.notify(db, beneficiary.id, "PRINT_GRANT_CHANGED", "Pages d'impression accordées",
                f"{user.username} vous accorde {body.amount} page(s) de son quota followme.")
    await db.commit()
    return (await _grants_out(db, [grant]))[0]


@router.patch("/grants/{grant_id}", response_model=GrantOut)
async def update_grant(grant_id: uuid.UUID, body: GrantPatch, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_user)):
    grant = await _own_grant(db, user, grant_id)
    if grant.revoked_at:
        raise PrintShareError("Cette autorisation a été retirée.", 409)
    if body.amount is not None:
        grant.amount = body.amount
    if "expires_at" in body.model_fields_set:
        grant.expires_at = _naive(body.expires_at)
    await db.commit()
    return (await _grants_out(db, [grant]))[0]


@router.delete("/grants/{grant_id}", response_model=GrantOut)
async def revoke_grant(grant_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    grant = await _own_grant(db, user, grant_id)
    await jobs.revoke_grant(db, user, grant)
    return (await _grants_out(db, [grant]))[0]


@router.get("/donations", response_model=list[RequestOut])
async def list_donations(limit: int = Query(default=100, le=500), db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_user)):
    """Every print made on the current user's account by others."""
    stmt = (select(PrintRequest).where(PrintRequest.donor_user_id == user.id)
            .order_by(PrintRequest.created_at.desc()).limit(limit))
    return await _requests_out(db, (await db.execute(stmt)).scalars().all())


@router.post("/requests/{request_id}/approve", response_model=RequestOut)
async def approve_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    return (await _requests_out(db, [await jobs.approve(db, user, request_id)]))[0]


@router.post("/requests/{request_id}/reject", response_model=RequestOut)
async def reject_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    return (await _requests_out(db, [await jobs.reject(db, user, request_id)]))[0]


# ── Requester ─────────────────────────────────────────────────────────────────

@router.get("/donors", response_model=list[DonorOut])
async def list_donors(db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    """Quotas the current user can print with: personal grants first, then open pools."""
    sharing = (
        await db.execute(select(PrintShareSettings).where(
            PrintShareSettings.is_sharing.is_(True),
            PrintShareSettings.is_paused.is_(False),
            PrintShareSettings.user_id != user.id,
        ))
    ).scalars().all()
    members = await _members(db, [s.user_id for s in sharing])
    out = []
    for settings in sharing:
        donor = members.get(settings.user_id)
        if not donor:
            continue
        grant = await quota.active_grant(db, settings.user_id, user.id)
        if grant:
            out.append(DonorOut(donor=donor, via="GRANT", grant_id=grant.id, grant_expires_at=grant.expires_at,
                                remaining=await quota.remaining(db, settings, user.id, grant),
                                auto_approve=settings.auto_approve))
        if settings.pool_enabled:
            out.append(DonorOut(donor=donor, via="POOL",
                                remaining=await quota.remaining(db, settings, user.id, None),
                                auto_approve=settings.auto_approve))
    out.sort(key=lambda d: (d.via != "GRANT", -d.remaining, d.donor.username))
    return out


@router.post("/requests", response_model=RequestOut, status_code=201)
async def create_request(
    file: UploadFile = File(...),
    donor_id: uuid.UUID = Form(...),
    via: str = Form("GRANT"),
    copies: int = Form(1),
    color: bool = Form(False),
    page_range: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    limit = jobs.app_settings.PRINT_MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    request = await jobs.submit(db, user, donor_id, data, file.filename or "document.pdf", copies=copies,
                                color=color, page_range=page_range, use_pool=via == "POOL")
    return (await _requests_out(db, [request]))[0]


@router.get("/requests", response_model=list[RequestOut])
async def list_my_requests(limit: int = Query(default=100, le=500), db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_user)):
    stmt = (select(PrintRequest).where(PrintRequest.requester_user_id == user.id)
            .order_by(PrintRequest.created_at.desc()).limit(limit))
    return await _requests_out(db, (await db.execute(stmt)).scalars().all())


@router.get("/requests/{request_id}", response_model=RequestOut)
async def get_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    request = await jobs.get_request(db, request_id)
    jobs._ensure_party(request, user)
    return (await _requests_out(db, [request]))[0]


@router.get("/requests/{request_id}/printers", response_model=list[PrinterOut])
async def list_printers(request_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    return await jobs.printers(db, user, request_id)


@router.post("/requests/{request_id}/release", response_model=RequestOut)
async def release_request(request_id: uuid.UUID, body: ReleaseIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_user)):
    return (await _requests_out(db, [await jobs.release(db, user, request_id, body.printer_name)]))[0]


@router.post("/requests/{request_id}/cancel", response_model=RequestOut)
async def cancel_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    return (await _requests_out(db, [await jobs.cancel(db, user, request_id)]))[0]


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.get("/admin/donors", response_model=list[DonorOverview])
async def admin_donors(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    all_settings = (await db.execute(select(PrintShareSettings))).scalars().all()
    members = await _members(db, [s.user_id for s in all_settings])
    out = []
    for settings in all_settings:
        grants = (await db.execute(select(PrintGrant).where(
            PrintGrant.donor_user_id == settings.user_id, PrintGrant.revoked_at.is_(None)))).scalars().all()
        out.append(DonorOverview(
            donor=members.get(settings.user_id) or _unknown(settings.user_id),
            settings=await _settings_out(db, settings),
            active_grants=sum(1 for g in grants if quota.grant_is_active(g)),
        ))
    return out


@router.put("/admin/donors/{donor_id}/pause", response_model=SettingsOut)
async def admin_pause_donor(donor_id: uuid.UUID, body: PauseIn, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    settings = await jobs.get_settings(db, donor_id)
    if not settings:
        raise PrintShareError("Ce membre n'a pas configuré de partage.", 404)
    settings.is_paused = body.is_paused
    await db.commit()
    return await _settings_out(db, settings)


@router.get("/admin/grants", response_model=list[GrantOut])
async def admin_grants(include_inactive: bool = False, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    stmt = select(PrintGrant).order_by(PrintGrant.created_at.desc())
    if not include_inactive:
        stmt = stmt.where(PrintGrant.revoked_at.is_(None))
    return await _grants_out(db, (await db.execute(stmt)).scalars().all())


@router.get("/admin/requests", response_model=list[RequestOut])
async def admin_requests(status: Optional[str] = None, limit: int = Query(default=200, le=1000),
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    stmt = select(PrintRequest).order_by(PrintRequest.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(PrintRequest.status == status)
    return await _requests_out(db, (await db.execute(stmt)).scalars().all())


@router.get("/admin/stats", response_model=StatsOut)
async def admin_stats(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """Aggregates only (no document names): used by the MCP server."""
    by_status = dict((await db.execute(
        select(PrintRequest.status, func.count()).group_by(PrintRequest.status))).all())
    printed = (await db.execute(select(func.coalesce(func.sum(PrintRequest.actual_cost), 0))
                                .where(PrintRequest.status == "RECONCILED"))).scalar_one()
    reserved = (await db.execute(select(func.coalesce(func.sum(PrintRequest.estimated_cost), 0))
                                 .where(PrintRequest.status.in_(quota.IN_FLIGHT + ("RELEASED",))))).scalar_one()
    grants = (await db.execute(select(PrintGrant).where(PrintGrant.revoked_at.is_(None)))).scalars().all()
    donors = (await db.execute(select(func.count()).select_from(PrintShareSettings)
                               .where(PrintShareSettings.is_sharing.is_(True)))).scalar_one()
    return StatsOut(
        sharing_donors=donors,
        active_grants=sum(1 for g in grants if quota.grant_is_active(g)),
        requests_by_status=by_status,
        pages_printed=int(printed),
        pages_reserved=int(reserved),
    )
