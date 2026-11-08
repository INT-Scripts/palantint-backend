import uuid

import pytest

from db.models import Organization


@pytest.mark.asyncio
async def test_public_clubs_hide_person_mentions(client, db_session):
    club_id = uuid.uuid4()
    db_session.add(
        Organization(
            id=club_id,
            kind="BUREAU",
            name="MiNET",
            slug="minet",
            description="L'association de réseau du campus.\n\nLe président est\n@[7a175537-0acf-4d1e-a440-46232c8236f1]",
        )
    )
    await db_session.commit()

    for path in ("/api/clubs", f"/api/clubs/{club_id}"):
        response = await client.get(path)
        assert response.status_code == 200
        data = response.json()
        club = data[0] if isinstance(data, list) else data
        assert club["description"] == "L'association de réseau du campus."
