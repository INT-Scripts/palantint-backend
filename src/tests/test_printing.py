import asyncio
import io
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from pypdf import PdfWriter
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.future import select
from tsprint.exceptions import LoginError

from core.auth import create_access_token, encrypt_secret, get_password_hash
from core.config import settings as app_settings
from db.models import Notification, PrintRequest, PrintShareSettings, User, UserCredential, utc_now
from services.printing import jobs, loop, session


class FakeFollowme:
    """In-memory stand-in for one donor's followme account."""

    state: dict = {}

    def __init__(self, username, password):
        self.username = username

    @property
    def account(self):
        return FakeFollowme.state.setdefault(self.username, {
            "balance": 100, "jobs": [], "released": [], "cancelled": [], "uploads": [], "logs": [], "fail_login": False,
        })

    async def login(self):
        if self.account["fail_login"]:
            raise LoginError("bad credentials")

    async def logout(self):
        pass

    async def aclose(self):
        pass

    async def get_webprint_printers(self):
        return ["underwood\\FollowMe-Students(virtuelle)", "underwood\\FollowMe-Students-Color(virtuelle)"]

    async def get_rates(self):
        rate = {"A4 (ISO_A4)": {"color": Decimal(8), "grayscale": Decimal(1), "color_duplex": Decimal(6), "grayscale_duplex": Decimal(1)}}
        return {"underwood\\FollowMe-Students": rate, "underwood\\FollowMe-Students-Color": rate}

    async def get_summary(self):
        return {"balance_value": Decimal(self.account["balance"])}

    async def upload_file(self, path, copies=1, printer_index=0, filename=None, **kwargs):
        self.account["uploads"].append({"name": filename, "copies": copies, "printer_index": printer_index})
        self.account["jobs"].append({"name": filename, "link": f"release/{filename}", "cancel_link": f"cancel/{filename}"})

    async def get_pending_jobs(self):
        return list(self.account["jobs"])

    async def get_physical_printers(self, job):
        return [{"name": "underwood\\MFP-1", "status": "OK", "link": "a"},
                {"name": "underwood\\MFP-2", "status": "Le périphérique est hors connexion", "link": "b"}]

    async def release_job(self, job, printer_name_filter=None):
        self.account["jobs"].remove(job)
        self.account["released"].append((job["name"], printer_name_filter))

    async def cancel_job(self, job):
        self.account["jobs"].remove(job)
        self.account["cancelled"].append(job["name"])

    async def get_print_logs(self, all_history=True):
        return list(self.account["logs"])


def make_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def fake_followme(monkeypatch, tmp_path):
    FakeFollowme.state = {}
    monkeypatch.setattr(session, "client_factory", FakeFollowme)
    monkeypatch.setattr(jobs, "FIND_JOB_DELAY", 0)
    monkeypatch.setattr(app_settings, "PRINT_UPLOAD_DIR", tmp_path / "print_uploads")
    jobs._queue_cache.update(at=0.0, queues=None)
    return FakeFollowme.state


async def make_user(db, username, *, cas_login=None, is_admin=False):
    user = User(id=uuid.uuid4(), username=username, hashed_password=get_password_hash("pw"), is_admin=is_admin)
    db.add(user)
    if cas_login:
        db.add(UserCredential(user_id=user.id, encrypted_username=encrypt_secret(cas_login),
                              encrypted_password=encrypt_secret("secret")))
    await db.commit()
    return user


def auth(user):
    return {"Authorization": f"Bearer {create_access_token(data={'sub': user.username, 'is_admin': user.is_admin})}"}


@pytest_asyncio.fixture
async def people(db_session):
    donor = await make_user(db_session, "donneur", cas_login="cas-donneur")
    requester = await make_user(db_session, "demandeur")
    other = await make_user(db_session, "autre")
    admin = await make_user(db_session, "admin", is_admin=True)
    return donor, requester, other, admin


async def share(client, donor, **overrides):
    body = {"is_sharing": True, "auto_approve": True, "pool_enabled": False, **overrides}
    res = await client.put("/api/private/print/settings", json=body, headers=auth(donor))
    assert res.status_code == 200, res.text
    return res.json()


async def grant(client, donor, beneficiary, amount):
    res = await client.post("/api/private/print/grants", json={"beneficiary_username": beneficiary.username, "amount": amount},
                            headers=auth(donor))
    assert res.status_code == 201, res.text
    return res.json()


async def submit(client, requester, donor, pages=1, **form):
    data = {"donor_id": str(donor.id), **{k: str(v) for k, v in form.items()}}
    return await client.post("/api/private/print/requests", data=data,
                             files={"file": ("Cours.pdf", make_pdf(pages), "application/pdf")}, headers=auth(requester))


@pytest.mark.asyncio
async def test_sharing_requires_cas_credentials(client, people):
    _, requester, _, _ = people
    res = await client.put("/api/private/print/settings", json={"is_sharing": True}, headers=auth(requester))
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_grant_print_release_and_reconcile(client, db_session, people, fake_followme):
    donor, requester, _, _ = people
    await share(client, donor)
    await grant(client, donor, requester, 5)

    donors = (await client.get("/api/private/print/donors", headers=auth(requester))).json()
    assert [(d["donor"]["username"], d["via"]) for d in donors] == [("donneur", "GRANT")]

    res = await submit(client, requester, donor, pages=2)
    assert res.status_code == 201, res.text
    request = res.json()
    assert request["status"] == "HELD"
    assert request["estimated_cost"] == 2
    account = fake_followme["cas-donneur"]
    assert account["uploads"][0]["printer_index"] == 0
    assert account["jobs"][0]["name"].startswith("palantint-") and account["jobs"][0]["name"].endswith("-Cours.pdf")
    assert list(app_settings.PRINT_UPLOAD_DIR.glob("*.pdf")) == []  # deleted once uploaded

    printers = (await client.get(f"/api/private/print/requests/{request['id']}/printers", headers=auth(requester))).json()
    assert [p["available"] for p in printers] == [True, False]

    offline = await client.post(f"/api/private/print/requests/{request['id']}/release",
                                json={"printer_name": "underwood\\MFP-2"}, headers=auth(requester))
    assert offline.status_code == 409
    res = await client.post(f"/api/private/print/requests/{request['id']}/release",
                            json={"printer_name": "underwood\\MFP-1"}, headers=auth(requester))
    assert res.json()["status"] == "RELEASED"
    assert account["released"] == [(account["uploads"][0]["name"], "underwood\\MFP-1")]

    account["logs"].append({"document": account["uploads"][0]["name"], "cost_value": Decimal(1)})
    await loop.reconcile(db_session)
    row = await db_session.get(PrintRequest, uuid.UUID(request["id"]))
    await db_session.refresh(row)
    assert (row.status, row.actual_cost) == ("RECONCILED", 1)

    grants = (await client.get("/api/private/print/grants", headers=auth(donor))).json()
    assert (grants[0]["used"], grants[0]["remaining"]) == (1, 4)
    donations = (await client.get("/api/private/print/donations", headers=auth(donor))).json()
    assert donations[0]["requester"]["username"] == "demandeur"


@pytest.mark.asyncio
async def test_color_reserves_worst_case_and_respects_grant(client, db_session, people, fake_followme):
    donor, requester, _, _ = people
    await share(client, donor)
    await grant(client, donor, requester, 10)

    res = await submit(client, requester, donor, pages=2, color="true")
    assert res.status_code == 409  # 2 colour pages = 16 > 10
    assert "insuffisante" in res.json()["detail"]
    assert (await db_session.execute(select(PrintRequest))).scalars().all() == []
    assert list(app_settings.PRINT_UPLOAD_DIR.glob("*.pdf")) == []

    res = await submit(client, requester, donor, pages=1, color="true")
    assert res.json()["estimated_cost"] == 8
    assert fake_followme["cas-donneur"]["uploads"][0]["printer_index"] == 1


@pytest.mark.asyncio
async def test_page_range_and_copies(client, people):
    donor, requester, _, _ = people
    await share(client, donor)
    await grant(client, donor, requester, 50)
    res = await submit(client, requester, donor, pages=5, page_range="2-3,5", copies=2)
    assert (res.json()["page_count"], res.json()["estimated_cost"]) == (3, 6)
    bad = await submit(client, requester, donor, pages=2, page_range="1-4")
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_balance_floor_and_global_cap(client, people, fake_followme):
    donor, requester, other, _ = people
    await share(client, donor, min_balance_floor=95, global_cap=3)
    await grant(client, donor, requester, 50)
    await grant(client, donor, other, 50)

    res = await submit(client, requester, donor, pages=2)
    assert res.status_code == 201
    res = await submit(client, other, donor, pages=2)
    assert res.status_code == 409 and "plafond" in res.json()["detail"]

    await share(client, donor, min_balance_floor=99, global_cap=None)
    res = await submit(client, other, donor, pages=1)  # 100 - 2 held - 1 < 99
    assert res.status_code == 409 and "solde" in res.json()["detail"]


@pytest.mark.asyncio
async def test_pool_with_per_person_cap(client, people):
    donor, requester, other, _ = people
    await share(client, donor, pool_enabled=True, pool_per_person_cap=2)
    donors = (await client.get("/api/private/print/donors", headers=auth(other))).json()
    assert [(d["via"], d["remaining"]) for d in donors] == [("POOL", 2)]

    assert (await submit(client, other, donor, pages=2, via="POOL")).status_code == 201
    assert (await submit(client, other, donor, pages=1, via="POOL")).status_code == 409
    assert (await submit(client, requester, donor, pages=2, via="POOL")).status_code == 201
    # No personal grant: the grant route is refused
    assert (await submit(client, requester, donor, pages=1)).status_code == 403


@pytest.mark.asyncio
async def test_manual_approval_and_expiry(client, db_session, people):
    donor, requester, _, _ = people
    await share(client, donor, auto_approve=False)
    await grant(client, donor, requester, 10)

    first = (await submit(client, requester, donor)).json()
    assert first["status"] == "PENDING_APPROVAL"
    pending = (await db_session.execute(select(Notification).where(Notification.user_id == donor.id))).scalars().all()
    assert [n.kind for n in pending] == ["PRINT_REQUEST_PENDING"]

    assert (await client.post(f"/api/private/print/requests/{first['id']}/approve", headers=auth(requester))).status_code == 403
    res = await client.post(f"/api/private/print/requests/{first['id']}/approve", headers=auth(donor))
    assert res.json()["status"] == "HELD"

    second = (await submit(client, requester, donor)).json()
    row = await db_session.get(PrintRequest, uuid.UUID(second["id"]))
    row.approval_expires_at = utc_now() - timedelta(minutes=1)
    await db_session.commit()
    await loop.expire_approvals(db_session)
    await db_session.refresh(row)
    assert row.status == "EXPIRED" and row.temp_path is None


@pytest.mark.asyncio
async def test_hold_warning_then_expiry_cancels_job(client, db_session, people, fake_followme):
    donor, requester, _, _ = people
    await share(client, donor)
    await grant(client, donor, requester, 10)
    request = (await submit(client, requester, donor)).json()
    row = await db_session.get(PrintRequest, uuid.UUID(request["id"]))

    row.hold_expires_at = utc_now() + timedelta(minutes=5)
    await db_session.commit()
    await loop.warn_holds(db_session)
    await loop.warn_holds(db_session)  # warned once only
    row.hold_expires_at = utc_now() - timedelta(minutes=1)
    await db_session.commit()
    await loop.expire_holds(db_session)

    await db_session.refresh(row)
    assert row.status == "EXPIRED"
    assert fake_followme["cas-donneur"]["cancelled"] == [row.queued_name]
    kinds = (await db_session.execute(select(Notification.kind).where(Notification.user_id == requester.id))).scalars().all()
    assert kinds.count("PRINT_HOLD_EXPIRING") == 1 and "PRINT_HOLD_EXPIRED" in kinds


@pytest.mark.asyncio
async def test_revoke_grant_cancels_held_job(client, db_session, people, fake_followme):
    donor, requester, _, _ = people
    await share(client, donor)
    created = await grant(client, donor, requester, 10)
    request = (await submit(client, requester, donor)).json()

    res = await client.delete(f"/api/private/print/grants/{created['id']}", headers=auth(donor))
    assert res.status_code == 200 and res.json()["is_active"] is False
    row = await db_session.get(PrintRequest, uuid.UUID(request["id"]))
    await db_session.refresh(row)
    assert row.status == "CANCELLED"
    assert fake_followme["cas-donneur"]["jobs"] == []
    assert (await submit(client, requester, donor)).status_code == 403


@pytest.mark.asyncio
async def test_pause_blocks_new_prints(client, people):
    donor, requester, _, admin = people
    await share(client, donor)
    await grant(client, donor, requester, 10)
    res = await client.put(f"/api/private/print/admin/donors/{donor.id}/pause", json={"is_paused": True}, headers=auth(admin))
    assert res.status_code == 200
    assert (await submit(client, requester, donor)).status_code == 403
    assert (await client.get("/api/private/print/donors", headers=auth(requester))).json() == []


@pytest.mark.asyncio
async def test_access_control_and_admin(client, people):
    donor, requester, other, admin = people
    await share(client, donor)
    await grant(client, donor, requester, 10)
    request = (await submit(client, requester, donor)).json()

    assert (await client.get(f"/api/private/print/requests/{request['id']}", headers=auth(other))).status_code == 403
    assert (await client.get("/api/private/print/admin/requests", headers=auth(requester))).status_code == 403

    res = await client.post(f"/api/private/print/requests/{request['id']}/cancel", headers=auth(admin))
    assert res.json()["status"] == "CANCELLED"
    stats = (await client.get("/api/private/print/admin/stats", headers=auth(admin))).json()
    assert stats["requests_by_status"] == {"CANCELLED": 1}
    assert "original_filename" not in str(stats)


@pytest.mark.asyncio
async def test_bad_donor_credentials_fail_only_the_request(client, db_session, people, fake_followme):
    donor, requester, _, _ = people
    await share(client, donor)
    await grant(client, donor, requester, 10)
    FakeFollowme("cas-donneur", "").account["fail_login"] = True
    res = await submit(client, requester, donor)
    assert res.status_code == 502
    settings = await db_session.get(PrintShareSettings, donor.id)
    await db_session.refresh(settings)
    assert settings.is_sharing is True and settings.is_paused is False


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_over_reserve(test_engine, client, people):
    donor, requester, _, _ = people
    await share(client, donor)
    await grant(client, donor, requester, 3)
    SessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

    async def attempt():
        async with SessionLocal() as db:
            try:
                await jobs.submit(db, requester, donor.id, make_pdf(2), "a.pdf")
                return "ok"
            except session.PrintShareError:
                return "refused"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["ok", "refused"]
