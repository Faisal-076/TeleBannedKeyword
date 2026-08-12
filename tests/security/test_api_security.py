"""API + security tests: auth required, no secrets leaked."""

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.services.chat_service import ChatService
from tests.conftest import FakeGateway

ADMIN_KEY = "test-admin-key-secret"
BOT_TOKEN = "123456:TESTTOKENabcdefghijklmnopqrstuvwxyz0123456789"
MASTER_SECRET = "test-master-secret-not-for-production"
API_HASH = "0123456789abcdef0123456789abcdef"


def _client(fake_gateway: FakeGateway) -> TestClient:
    gateway = fake_gateway
    chat_service = ChatService(gateway)
    app = create_app(gateway=gateway, chat_service=chat_service)
    return TestClient(app)


async def test_health_ok(db, fake_gateway):
    client = _client(fake_gateway)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] in ("ok", "error")
    assert "mtproto" in body


async def test_ready(db, fake_gateway):
    client = _client(fake_gateway)
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        assert response.json()["ready"] is True


async def test_admin_requires_bearer(db, fake_gateway):
    client = _client(fake_gateway)
    assert client.get("/api/v1/admin/chats").status_code in (401, 503)
    bad = client.get(
        "/api/v1/admin/chats", headers={"Authorization": "Bearer wrong-key"}
    )
    assert bad.status_code in (401, 403)


async def test_admin_crud_flow(db, fake_gateway):
    fake_gateway.chat_meta[CHAT_ID] = {
        "id": CHAT_ID, "title": "Public Group", "username": "publicgroup",
        "chat_type": "group",
    }
    client = _client(fake_gateway)
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}

    added = client.post("/api/v1/admin/chats", json={"reference": "@publicgroup"}, headers=headers)
    assert added.status_code == 200
    assert added.json()["ok"] is True
    assert added.json()["chat_id"] == CHAT_ID

    listed = client.get("/api/v1/admin/chats", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()["chats"]) == 1

    rule = client.post(
        "/api/v1/admin/rules",
        json={"kind": "regex", "pattern": r"\bscam\b", "category": "scam"},
        headers=headers,
    )
    assert rule.status_code == 200
    rule_id = rule.json()["id"]

    bad_regex = client.post(
        "/api/v1/admin/rules", json={"kind": "regex", "pattern": "([bad"}, headers=headers
    )
    assert bad_regex.status_code == 400

    deleted = client.delete(f"/api/v1/admin/rules/{rule_id}", headers=headers)
    assert deleted.json()["deleted"] is True

    removed = client.delete(f"/api/v1/admin/chats/{CHAT_ID}", headers=headers)
    assert removed.json()["removed"] is True


async def test_health_exposes_no_secrets(db, fake_gateway):
    client = _client(fake_gateway)
    body = client.get("/health").text
    assert BOT_TOKEN not in body
    assert ADMIN_KEY not in body
    assert MASTER_SECRET not in body
    assert API_HASH not in body


async def test_add_inaccessible_chat_rejected_gracefully(db, fake_gateway):
    client = _client(fake_gateway)
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    response = client.post(
        "/api/v1/admin/chats", json={"reference": "@nonexistentgroup"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"]


CHAT_ID = -100777
