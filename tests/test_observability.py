"""HW2 authentication tests. Offline: no Langfuse, Docker, or model key."""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import app as server_app


def test_create_session_rejects_role_mismatch(world: dict) -> None:
    client = TestClient(server_app.app)
    response = client.post("/sessions", json={"user_id": 9002, "role": "shopper"})
    assert response.status_code == 403


def test_token_cannot_authorize_another_session(world: dict) -> None:
    client = TestClient(server_app.app)
    first = client.post("/sessions", json={"user_id": 1, "role": "shopper"}).json()
    second = client.post("/sessions", json={"user_id": 2, "role": "shopper"}).json()
    response = client.post(
        f"/sessions/{second['session_id']}/messages",
        headers={"Authorization": f"Bearer {first['token']}"},
        json={"message": "Show my recent orders."},
    )
    assert response.status_code == 403
