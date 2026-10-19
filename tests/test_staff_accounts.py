"""Per-person staff accounts with roles, sign-in tokens, revocation and the audit trail."""
import time

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.staff import StaffDirectory, StaffError, check_password, hash_password
from tests.helpers import Phone
from tests.test_api import make_settings

PIN = {"X-Staff-Pin": "4321"}
MILK = "8901000000066"


def bearer(c, username, password):
    r = c.post("/api/staff/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        for user, role in (("asha", "associate"), ("ravi", "supervisor"), ("meera", "admin")):
            r = c.post("/api/staff/users", headers=PIN,
                       json={"username": user, "name": user.title(), "role": role, "password": f"{user}-password"})
            assert r.status_code == 200, r.text
        yield c


def test_passwords_are_hashed_with_scrypt():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert check_password("correct horse", stored) and not check_password("wrong", stored)


def test_roles_gate_what_each_person_can_do(client):
    asha, ravi, meera = (bearer(client, u, f"{u}-password") for u in ("asha", "ravi", "meera"))
    assert client.get("/api/staff/me", headers=asha).json()["role"] == "associate"
    assert client.get("/api/ops/summary", headers=asha).status_code == 200
    assert client.post("/api/ops/network", json={"online": True}, headers=asha).status_code == 403
    assert client.post("/api/ops/network", json={"online": True}, headers=ravi).status_code == 200
    assert client.get("/api/staff/users", headers=ravi).status_code == 403
    assert {u["username"] for u in client.get("/api/staff/users", headers=meera).json()} == {"asha", "ravi", "meera"}
    assert client.get("/api/ops/summary").status_code == 401


def test_wrong_password_and_unknown_user_look_the_same(client):
    a = client.post("/api/staff/login", json={"username": "asha", "password": "nope-nope"})
    b = client.post("/api/staff/login", json={"username": "ghost", "password": "nope-nope"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_disabling_or_changing_role_ends_existing_sign_ins(client):
    asha, meera = bearer(client, "asha", "asha-password"), bearer(client, "meera", "meera-password")
    assert client.post("/api/staff/users/asha", json={"role": "supervisor"}, headers=meera).status_code == 200
    assert client.get("/api/ops/summary", headers=asha).status_code == 401  # old token: role changed
    asha = bearer(client, "asha", "asha-password")
    assert client.post("/api/ops/network", json={"online": True}, headers=asha).status_code == 200
    client.post("/api/staff/users/asha", json={"active": False}, headers=meera)
    assert client.get("/api/ops/summary", headers=asha).status_code == 401
    assert client.post("/api/staff/login", json={"username": "asha", "password": "asha-password"}).status_code == 401
    assert client.post("/api/staff/users/meera", json={"active": False}, headers=meera).status_code == 400


def test_staff_actions_are_audit_logged_by_name(client):
    ravi = bearer(client, "ravi", "ravi-password")
    p = Phone(client)
    p.scan(MILK)
    assert client.post(f"/api/ops/trips/{p.sid}/verify-id", headers=ravi).status_code == 200
    rows = [r for r in client.app.state.engine.ledger._db.execute(
        "SELECT kind, session_id, payload FROM events WHERE kind='staff.action'")]
    texts = [r[2] for r in rows]
    assert any('"staff": "ravi"' in t and "trips/" in t for t in texts)
    assert any(r[1] == p.sid for r in rows)  # tied to the trip
    assert client.app.state.engine.ledger.verify_chain()


def test_ops_socket_accepts_a_staff_token(client):
    asha = bearer(client, "asha", "asha-password")["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/ops?token={asha}"):
        pass
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/ops?token=forged.token") as ws:
            ws.receive_text()


def test_directory_rules(tmp_path):
    d = StaffDirectory(tmp_path)
    with pytest.raises(StaffError):
        d.add("x", "X", "associate", "longenough", 0)  # username too short
    with pytest.raises(StaffError):
        d.add("sam", "Sam", "boss", "longenough", 0)
    with pytest.raises(StaffError):
        d.add("sam", "Sam", "associate", "short", 0)
    d.add("sam", "Sam", "associate", "longenough", 0)
    with pytest.raises(StaffError):
        d.add("SAM", "Sam", "associate", "longenough", 0)  # usernames are case-insensitive
    token, _ = d.login("sam", "longenough", time.time())
    with pytest.raises(StaffError):
        d.verify(token, time.time() + 13 * 3600)  # expired
