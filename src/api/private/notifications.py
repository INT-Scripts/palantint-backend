import uuid
import asyncio
import httpx
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.future import select

from api.private.deps import User, require_user
from db.database import get_db, AsyncSessionLocal
from db.models import LaundrySubscription, Location, Notification
from api.public.laundry import LAUNDRY_URLS, HEADERS

router = APIRouter(prefix="/notifications", tags=["notifications"])


class LaundrySubscribeRequest(BaseModel):
    building: str
    machine_nbr: int


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    message: str
    is_read: bool
    created_at: datetime


async def get_or_create_machine_slot(db: AsyncSession, building: str, machine_nbr: int) -> Location:
    """Resolve (or lazily create) the Location(kind=MACHINE_SLOT) for a given
    building/machine number pair. The laundry ingestion pipeline is a separate
    workstream and doesn't seed these yet, so the subscribe endpoint creates
    them on demand under a BUILDING-kind parent keyed by the building code.

    The BUILDING code is kept upper-cased to match the convention used
    everywhere else a BUILDING Location is created/looked up (apartments.py's
    ingestion, api/private/maps.py, api/public/maps.py's BUILDING_FLOORS) --
    a prior version of this function lower-cased it, which silently created a
    second, disconnected BUILDING row (e.g. "u3" alongside "U3") for the same
    physical building instead of reusing the existing one.
    """
    building_key = building.strip().upper()

    result = await db.execute(
        select(Location).where(Location.kind == "BUILDING", Location.code == building_key)
    )
    building_loc = result.scalars().first()
    if not building_loc:
        building_loc = Location(kind="BUILDING", code=building_key, name=building_key)
        db.add(building_loc)
        await db.flush()

    result = await db.execute(
        select(Location).where(
            Location.kind == "MACHINE_SLOT",
            Location.parent_id == building_loc.id,
            Location.code == str(machine_nbr),
        )
    )
    slot = result.scalars().first()
    if not slot:
        slot = Location(
            kind="MACHINE_SLOT",
            code=str(machine_nbr),
            parent_id=building_loc.id,
            name=f"Machine {machine_nbr}",
        )
        db.add(slot)
        await db.flush()
    return slot


@router.post("/laundry/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe_laundry(
    payload: LaundrySubscribeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    """Subscribes the current user to a laundry machine's availability."""
    slot = await get_or_create_machine_slot(db, payload.building, payload.machine_nbr)

    # Check if subscription already exists and is active
    stmt = select(LaundrySubscription).where(
        LaundrySubscription.user_id == current_user.id,
        LaundrySubscription.location_id == slot.id,
        LaundrySubscription.is_active == True
    )
    res = await db.execute(stmt)
    existing = res.scalars().first()
    if existing:
        return {"status": "already_subscribed", "subscription_id": existing.id}

    subscription = LaundrySubscription(
        user_id=current_user.id,
        location_id=slot.id,
        is_active=True
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return {"status": "subscribed", "subscription_id": subscription.id}


@router.get("", response_model=List[NotificationResponse])
async def get_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    """Returns all notifications for the current user, sorted by newest first."""
    stmt = select(Notification).where(
        Notification.user_id == current_user.id
    ).order_by(Notification.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    """Marks a specific notification as read."""
    stmt = select(Notification).where(
        Notification.id == notification_id,
        Notification.user_id == current_user.id
    )
    res = await db.execute(stmt)
    notification = res.scalars().first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    notification.is_read = True
    await db.commit()
    return {"status": "success"}


# ── Background Notifier Task ──────────────────────────────────────────────────

async def check_laundry_subscriptions():
    async with AsyncSessionLocal() as db:
        # Get active subscriptions, eagerly loading the machine slot Location
        # (and its parent BUILDING Location) so we can group by building code
        # without N+1 queries.
        stmt = (
            select(LaundrySubscription)
            .options(selectinload(LaundrySubscription.location).selectinload(Location.parent))
            .where(LaundrySubscription.is_active == True)
        )
        res = await db.execute(stmt)
        subscriptions = res.scalars().all()

        if not subscriptions:
            return

        # Group by building code (the machine slot's parent Location)
        by_building = {}
        for sub in subscriptions:
            slot = sub.location
            if not slot or not slot.parent:
                continue
            by_building.setdefault(slot.parent.code, []).append(sub)

        async with httpx.AsyncClient() as client:
            for building, subs in by_building.items():
                # Location codes are upper-cased (e.g. "U3"); LAUNDRY_URLS keys are lower-cased.
                b_key = building.lower()
                if b_key not in LAUNDRY_URLS:
                    continue

                url = LAUNDRY_URLS[b_key]
                try:
                    response = await client.get(url, headers=HEADERS, timeout=10.0)
                    if response.status_code != 200:
                        continue
                    machines = response.json()
                except Exception as e:
                    print(f"Error fetching laundry status for notification check of {building}: {e}")
                    continue

                for sub in subs:
                    machine_nbr = int(sub.location.code)
                    # Find machine in the response list
                    target_machine = None
                    for m in machines:
                        if m.get("machine_nbr") == machine_nbr:
                            target_machine = m
                            break

                    if target_machine:
                        started_at = target_machine.get("started_at")
                        if not started_at:  # Machine is free!
                            # Create Notification
                            notification = Notification(
                                user_id=sub.user_id,
                                title="Laundry Machine Available!",
                                message=f"Machine {machine_nbr} ({'Dryer' if target_machine.get('machine_type') == 'sl' else 'Washer'}) in building {building.upper()} is now available.",
                                is_read=False
                            )
                            db.add(notification)
                            # Mark subscription as inactive
                            sub.is_active = False

        await db.commit()


async def laundry_notifier_loop():
    while True:
        try:
            await check_laundry_subscriptions()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error in laundry notifier task: {e}")
        await asyncio.sleep(30)  # run every 30 seconds
