async def test_index(client) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "astra-backend"
