import json
import os
import uuid
import pytest

from db.models import Location, MapMetadata


@pytest.mark.asyncio
async def test_save_map_metadata_requires_admin(client, user_token):
    # Unauthenticated
    resp = await client.post("/api/private/maps/U1/0/metadata", json={"pillars": []})
    assert resp.status_code == 401

    # Regular user (not admin) -> 403 Forbidden
    headers = {"Authorization": f"Bearer {user_token}"}
    resp = await client.post("/api/private/maps/U1/0/metadata", json={"pillars": []}, headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_save_and_get_map_metadata(client, db_session, admin_token, user_token):
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_headers = {"Authorization": f"Bearer {user_token}"}

    payload = {
        "pillars": [
            {"x": 12.34, "y": 56.78},
            {"x": 87.65, "y": 43.21}
        ]
    }

    # Save metadata as admin
    resp = await client.post("/api/private/maps/U7/2/metadata", json=payload, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}

    # Fetch private metadata as user
    resp = await client.get("/api/private/maps/U7/2/metadata", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["pillars"]) == 2
    assert pytest.approx(data["pillars"][0]["x"]) == 12.34
    assert pytest.approx(data["pillars"][0]["y"]) == 56.78
    assert pytest.approx(data["pillars"][1]["x"]) == 87.65
    assert pytest.approx(data["pillars"][1]["y"]) == 43.21

    # Fetch public metadata without auth
    resp = await client.get("/api/maps/U7/2/metadata")
    assert resp.status_code == 200
    public_data = resp.json()
    assert len(public_data["pillars"]) == 2

    # Fetch building metadata
    resp = await client.get("/api/private/maps/U7/metadata", headers=user_headers)
    assert resp.status_code == 200
    bldg_data = resp.json()
    assert "2" in bldg_data
    assert len(bldg_data["2"]["pillars"]) == 2


@pytest.mark.asyncio
async def test_update_existing_map_metadata(client, db_session, admin_token, user_token):
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_headers = {"Authorization": f"Bearer {user_token}"}

    # First save
    payload1 = {
        "pillars": [{"x": 10.0, "y": 20.0}]
    }
    resp1 = await client.post("/api/private/maps/U1/1/metadata", json=payload1, headers=admin_headers)
    assert resp1.status_code == 200

    # Update with new pillars
    payload2 = {
        "pillars": [
            {"x": 15.0, "y": 25.0},
            {"x": 35.0, "y": 45.0}
        ]
    }
    resp2 = await client.post("/api/private/maps/U1/1/metadata", json=payload2, headers=admin_headers)
    assert resp2.status_code == 200

    # Verify updated
    resp = await client.get("/api/private/maps/U1/1/metadata", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["pillars"]) == 2
    assert pytest.approx(data["pillars"][0]["x"]) == 15.0
    assert pytest.approx(data["pillars"][1]["x"]) == 35.0


@pytest.mark.asyncio
async def test_save_map_metadata_multiple_buildings_same_floor(client, db_session, admin_token, user_token):
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_headers = {"Authorization": f"Bearer {user_token}"}

    # Save floor 1 for U1
    resp_u1 = await client.post(
        "/api/private/maps/U1/1/metadata",
        json={"pillars": [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]},
        headers=admin_headers
    )
    assert resp_u1.status_code == 200

    # Save floor 1 for U7 (must not violate unique constraint across different buildings)
    resp_u7 = await client.post(
        "/api/private/maps/U7/1/metadata",
        json={"pillars": [{"x": 10.0, "y": 20.0}, {"x": 30.0, "y": 40.0}]},
        headers=admin_headers
    )
    assert resp_u7.status_code == 200

    # Verify both exist independently
    meta_u1 = await client.get("/api/private/maps/U1/1/metadata", headers=user_headers)
    assert meta_u1.status_code == 200
    assert pytest.approx(meta_u1.json()["pillars"][0]["x"]) == 1.0

    meta_u7 = await client.get("/api/private/maps/U7/1/metadata", headers=user_headers)
    assert meta_u7.status_code == 200
    assert pytest.approx(meta_u7.json()["pillars"][0]["x"]) == 10.0
