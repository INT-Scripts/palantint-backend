from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_laundry_status_requires_auth(client):
    response = await client.get("/api/private/laundry/status")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_laundry_status_aggregate_across_all_buildings(client, user_token):
    headers = {"Authorization": f"Bearer {user_token}"}

    async def fake_fetch(building: str):
        return [
            {
                "machine_nbr": 1,
                "machine_type": "washer",
                "started_at": None,
                "_building": building,
            }
        ]

    with patch(
        "api.private.laundry.fetch_building_status",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        response = await client.get("/api/private/laundry/status", headers=headers)

    assert response.status_code == 200
    data = response.json()
    # One entry per configured building.
    assert set(data.keys()) == {"u3", "u4", "u5", "u6", "u7"}
    for building, machines in data.items():
        assert isinstance(machines, list)
        assert machines[0]["_building"] == building


@pytest.mark.asyncio
async def test_laundry_status_single_building_filter(client, user_token):
    headers = {"Authorization": f"Bearer {user_token}"}

    async def fake_fetch(building: str):
        return [
            {
                "machine_nbr": 42,
                "machine_type": "dryer",
                "started_at": "2026-07-30T10:00:00",
            }
        ]

    with patch(
        "api.private.laundry.fetch_building_status",
        new=AsyncMock(side_effect=fake_fetch),
    ) as mocked:
        response = await client.get(
            "/api/private/laundry/status", params={"building": "U3"}, headers=headers
        )

    assert response.status_code == 200
    data = response.json()
    assert list(data.keys()) == ["u3"]
    assert data["u3"][0]["machine_nbr"] == 42
    mocked.assert_awaited_once_with("U3")


@pytest.mark.asyncio
async def test_laundry_status_aggregate_survives_one_building_failure(
    client, user_token
):
    """A single building's fetch failing (e.g. Touch'n'Pay API down/rate-limited)
    must not take down the whole aggregate response -- that building should
    come back as [], and every other building's real data should still be present.
    """
    headers = {"Authorization": f"Bearer {user_token}"}

    async def fake_fetch(building: str):
        if building == "u4":
            raise HTTPException(status_code=502, detail="Touch'n'Pay API error")
        return [{"machine_nbr": 1, "machine_type": "washer", "started_at": None}]

    with patch(
        "api.private.laundry.fetch_building_status",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        response = await client.get("/api/private/laundry/status", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["u4"] == []
    for building in ("u3", "u5", "u6", "u7"):
        assert data[building] != []


@pytest.mark.asyncio
async def test_public_laundry_coalesces_requests_and_uses_cache(monkeypatch):
    import asyncio

    from api.public import laundry

    calls = []
    monkeypatch.setattr(laundry, "LAUNDRY_CACHE", {})
    monkeypatch.setattr(
        laundry, "_locks", __import__("collections").defaultdict(asyncio.Lock)
    )

    def provider(url):
        calls.append(url)
        return [{"machine_nbr": "1", "machine_state": "0"}]

    monkeypatch.setattr(laundry, "_request_provider", provider)
    results = await asyncio.gather(
        *(laundry.fetch_building_status("U3") for _ in range(8))
    )
    assert len(calls) == 1
    assert all(result == results[0] for result in results)


@pytest.mark.asyncio
async def test_provider_timeout_returns_503_instead_of_fake_empty_machines(
    client, monkeypatch
):
    from api.public import laundry

    monkeypatch.setattr(laundry, "LAUNDRY_CACHE", {})

    def unavailable(url):
        raise TimeoutError("provider TLS timeout")

    monkeypatch.setattr(laundry, "_request_provider", unavailable)
    response = await client.get("/api/laundry/u3")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


def test_provider_mss_is_advertised_before_connect_and_tls_is_verified(monkeypatch):
    import socket
    import ssl
    from unittest.mock import Mock

    from api.public.laundry import LaundryHTTPSConnection

    calls = []
    raw = Mock()
    raw.setsockopt.side_effect = lambda *args: calls.append("mss")
    raw.connect.side_effect = lambda *args: calls.append("connect")
    context = Mock()
    context.wrap_socket.side_effect = lambda *args, **kwargs: (
        calls.append("tls") or Mock()
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    monkeypatch.setattr(socket, "socket", lambda *args: raw)
    connection = LaundryHTTPSConnection("api.touchnpay.fr", context=context, timeout=8)
    connection.connect()
    assert calls == ["mss", "connect", "tls"]
    context.wrap_socket.assert_called_once_with(raw, server_hostname="api.touchnpay.fr")
    secure = LaundryHTTPSConnection("api.touchnpay.fr")
    assert secure._context.verify_mode == ssl.CERT_REQUIRED
    assert secure._context.check_hostname
