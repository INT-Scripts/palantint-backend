import uuid
import asyncio
import httpx
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.private.deps import User, require_user
from db.database import get_db, AsyncSessionLocal
from db.models import LaundrySubscription, Notification
from api.public.laundry import LAUNDRY_URLS, HEADERS

router = APIRouter(prefix="/notifications", tags=["notifications"])


class LaundrySubscribeRequest(BaseModel):
    building: str
    machine_nbr: int


class NotificationResponse(BaseModel):
    id: uuid.UUID
    title: str
    message: str
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/laundry/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe_laundry(
    payload: LaundrySubscribeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_user)
):
    """Subscribes the current user to a laundry machine's availability."""
    # Check if subscription already exists and is active
    stmt = select(LaundrySubscription).where(
        LaundrySubscription.user_id == current_user.id,
        LaundrySubscription.building == payload.building.lower(),
        LaundrySubscription.machine_nbr == payload.machine_nbr,
        LaundrySubscription.is_active == True
    )
    res = await db.execute(stmt)
    existing = res.scalars().first()
    if existing:
        return {"status": "already_subscribed", "subscription_id": existing.id}

    subscription = LaundrySubscription(
        user_id=current_user.id,
        building=payload.building.lower(),
        machine_nbr=payload.machine_nbr,
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
        # Get active subscriptions
        stmt = select(LaundrySubscription).where(LaundrySubscription.is_active == True)
        res = await db.execute(stmt)
        subscriptions = res.scalars().all()

        if not subscriptions:
            return

        # Group by building
        by_building = {}
        for sub in subscriptions:
            by_building.setdefault(sub.building, []).append(sub)

        async with httpx.AsyncClient() as client:
            for building, subs in by_building.items():
                if building not in LAUNDRY_URLS:
                    continue

                url = LAUNDRY_URLS[building]
                try:
                    response = await client.get(url, headers=HEADERS, timeout=10.0)
                    if response.status_code != 200:
                        continue
                    machines = response.json()
                except Exception as e:
                    print(f"Error fetching laundry status for notification check of {building}: {e}")
                    continue

                for sub in subs:
                    # Find machine in the response list
                    target_machine = None
                    for m in machines:
                        if m.get("machine_nbr") == sub.machine_nbr:
                            target_machine = m
                            break

                    if target_machine:
                        started_at = target_machine.get("started_at")
                        if not started_at:  # Machine is free!
                            # Create Notification
                            notification = Notification(
                                user_id=sub.user_id,
                                title="Laundry Machine Available!",
                                message=f"Machine {sub.machine_nbr} ({'Dryer' if target_machine.get('machine_type') == 'sl' else 'Washer'}) in building {building.upper()} is now available.",
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
