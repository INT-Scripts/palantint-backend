import pytest

from core.auth import create_access_token, get_password_hash
from db.models import User

URL = "/api/private/maps/indoor/bookmarks"


@pytest.mark.asyncio
async def test_bookmarks_are_private(client):
    assert (await client.get(URL)).status_code == 401


@pytest.mark.asyncio
async def test_bookmarks_belong_to_their_user_and_update_in_place(client, db_session, user_token):
    other = User(username="other", hashed_password=get_password_hash("x"))
    db_session.add(other)
    await db_session.commit()
    mine, theirs = {"Authorization": f"Bearer {user_token}"}, {"Authorization": f"Bearer {create_access_token({'sub': 'other'})}"}

    flat = {"feature_id": "plan:U7_1:room:7106", "name": "7106", "building_id": "1360", "level": 1}
    assert (await client.put(URL, json=flat, headers=mine)).json()["name"] == "7106"
    await client.put(URL, json={"feature_id": "features:indoorfeature:1:Polygon", "name": "A008", "building_id": "1346", "level": 0}, headers=mine)
    # Bookmarking again refreshes the stored name instead of duplicating.
    await client.put(URL, json={**flat, "name": "7106 · studio"}, headers=mine)

    listed = (await client.get(URL, headers=mine)).json()["bookmarks"]
    assert [item["name"] for item in listed] == ["A008", "7106 · studio"]
    assert (await client.get(URL, headers=theirs)).json()["bookmarks"] == []

    await client.delete(URL, params={"feature_id": flat["feature_id"]}, headers=theirs)
    assert len((await client.get(URL, headers=mine)).json()["bookmarks"]) == 2
    await client.delete(URL, params={"feature_id": flat["feature_id"]}, headers=mine)
    assert [item["name"] for item in (await client.get(URL, headers=mine)).json()["bookmarks"]] == ["A008"]
