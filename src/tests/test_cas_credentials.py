import pytest
from sqlalchemy.future import select

from core.auth import decrypt_secret
from db.models import UserCredential


@pytest.mark.asyncio
async def test_cas_credentials_round_trip_and_are_actually_encrypted(client, db_session, user_token, test_user):
    headers = {"Authorization": f"Bearer {user_token}"}
    plaintext_username = "jdoe"
    plaintext_password = "s3cr3t-p@ssw0rd"

    post_res = await client.post(
        "/api/private/users/me/cas-credentials",
        json={"cas_username": plaintext_username, "cas_password": plaintext_password},
        headers=headers,
    )
    assert post_res.status_code == 200
    assert post_res.json() == {"status": "success"}

    # Round trip via the API
    get_res = await client.get("/api/private/users/me/cas-credentials", headers=headers)
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["has_credentials"] is True
    assert data["cas_username"] == plaintext_username

    # Inspect the raw DB row directly -- the encrypted bytes must NOT be the
    # plaintext (nor a trivial encoding of it like base64), and must be
    # Fernet-decryptable back to the original values.
    result = await db_session.execute(
        select(UserCredential).where(UserCredential.user_id == test_user.id)
    )
    credential = result.scalars().first()
    assert credential is not None

    assert credential.encrypted_username != plaintext_username.encode()
    assert credential.encrypted_password != plaintext_password.encode()
    assert plaintext_username.encode() not in credential.encrypted_username
    assert plaintext_password.encode() not in credential.encrypted_password

    assert decrypt_secret(credential.encrypted_username) == plaintext_username
    assert decrypt_secret(credential.encrypted_password) == plaintext_password


@pytest.mark.asyncio
async def test_cas_credentials_get_without_saved_credentials(client, user_token):
    headers = {"Authorization": f"Bearer {user_token}"}
    get_res = await client.get("/api/private/users/me/cas-credentials", headers=headers)
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["has_credentials"] is False
    assert data["cas_username"] == ""


@pytest.mark.asyncio
async def test_cas_credentials_update_overwrites_previous(client, db_session, user_token, test_user):
    headers = {"Authorization": f"Bearer {user_token}"}
    await client.post(
        "/api/private/users/me/cas-credentials",
        json={"cas_username": "first_user", "cas_password": "first_pass"},
        headers=headers,
    )
    await client.post(
        "/api/private/users/me/cas-credentials",
        json={"cas_username": "second_user", "cas_password": "second_pass"},
        headers=headers,
    )

    result = await db_session.execute(
        select(UserCredential).where(UserCredential.user_id == test_user.id)
    )
    rows = result.scalars().all()
    # Exactly one row -- updated in place, not a second credential row.
    assert len(rows) == 1
    assert decrypt_secret(rows[0].encrypted_username) == "second_user"


@pytest.mark.asyncio
async def test_cas_credentials_require_auth(client):
    response = await client.get("/api/private/users/me/cas-credentials")
    assert response.status_code == 401

    response = await client.post(
        "/api/private/users/me/cas-credentials",
        json={"cas_username": "x", "cas_password": "y"},
    )
    assert response.status_code == 401
