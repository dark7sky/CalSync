import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, normalize_event, db
from app.security import create_user


def event(**overrides):
    base = {
        "type": "not-used",
        "uid": "not-used",
        "calendarId": "must-be-ignored",
        "attendees": [{"email": "ignored@example.com"}],
        "URL": "https://ignored.example.org",
        "subject": "예시 회의",
        "startTime": {"date": None, "dateTime": "2026-11-12T10:00:00+09:00", "timeZone": "Asia/Seoul"},
        "endTime": {"date": None, "dateTime": "2026-11-12T11:00:00+09:00", "timeZone": "Asia/Seoul"},
        "chair": {"displayName": "가상 사용자", "id": "123", "email": "fake@example.com"},
        "location": "회의실 A",
    }
    base.update(overrides)
    return base


def test_five_input_keys_only():
    result = normalize_event(event())
    assert result["summary"] == "예시 회의"
    assert result["description"] == "주최: 가상 사용자"
    assert result["location"] == "회의실 A"
    assert "attendees" not in result
    assert "calendarId" not in result
    assert "uid" not in result
    assert result["start"]["timeZone"] == "Asia/Seoul"


def test_invalid_end_precedes_start():
    with pytest.raises(ValueError):
        normalize_event(event(endTime={
            "dateTime": "2026-11-12T09:00:00+09:00", "timeZone": "Asia/Seoul"
        }))


def test_all_day_with_exclusive_end():
    obj = event(
        startTime={"date": "2026-12-01", "dateTime": None, "timeZone": "Asia/Seoul"},
        endTime={"date": "2026-12-02", "dateTime": None, "timeZone": "Asia/Seoul"},
    )
    result = normalize_event(obj)
    assert result["start"] == {"date": "2026-12-01"}
    assert result["end"] == {"date": "2026-12-02"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALSYNC_CALENDAR_ID", "example@example.com")
    monkeypatch.setenv("CALSYNC_PUBLIC_ORIGIN", "https://calendar.example.com")
    monkeypatch.setenv("CALSYNC_DATABASE", str(tmp_path / "events.db"))
    with TestClient(app, base_url="https://calendar.example.com") as c:
        with db() as conn:
            create_user(conn, "submitter", "super-secret-password-2026", "submitter")
            create_user(conn, "otheruser", "super-secret-password-2026", "submitter")
            create_user(conn, "viewer", "super-secret-password-2026", "viewer")
            create_user(conn, "admin", "super-secret-password-2026", "admin")
        yield c


def test_authenticated_browser_import_and_idempotency(client):
    assert client.get("/api/v1/requests").status_code == 401
    response = client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"}, follow_redirects=False)
    assert response.status_code == 303
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    key = "batch" + uuid.uuid4().hex
    payload = {"batchId": key, "events": [event()]}
    headers = {"X-CSRF-Token": csrf, "Origin": "https://calendar.example.com"}
    first = client.post("/api/v1/requests/import", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["requests"][0]["status"] == "PENDING"
    again = client.post("/api/v1/requests/import", json=payload, headers=headers)
    assert again.status_code == 200
    assert first.json()["requests"] == again.json()["requests"]
    changed = {"batchId": key, "events": [event(subject="다른 제목")]}
    conflict = client.post("/api/v1/requests/import", json=changed, headers=headers)
    assert conflict.status_code == 409


def test_csrf_and_admin_required(client):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"}, follow_redirects=False)
    key = "batch" + uuid.uuid4().hex
    payload = {"batchId": key, "events": [event()]}
    assert client.post("/api/v1/requests/import", json=payload).status_code == 403
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    headers = {"X-CSRF-Token": csrf, "Origin": "https://calendar.example.com"}
    rid = client.post("/api/v1/requests/import", json=payload, headers=headers).json()["requests"][0]["requestId"]
    assert client.post(f"/api/v1/admin/requests/{rid}/reject", headers=headers).status_code == 403



def test_login_wrong_password_and_security_headers(client):
    wrong = client.post("/login", data={"username": "submitter", "password": "wrong"})
    assert wrong.status_code == 401
    response = client.get("/login")
    assert response.status_code == 200
    assert "max-age" in response.headers["strict-transport-security"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    logged = client.post("/login", data={
        "username": "submitter", "password": "super-secret-password-2026"
    }, follow_redirects=False)
    assert logged.status_code == 303
    cookie = logged.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_viewer_cannot_submit_and_users_only_see_own_requests(client):
    auth_data = {"username": "submitter", "password": "super-secret-password-2026"}
    client.post("/login", data=auth_data, follow_redirects=False)
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    header = {"X-CSRF-Token": csrf, "Origin": "https://calendar.example.com"}
    key = "batch" + uuid.uuid4().hex
    result = client.post("/api/v1/requests/import",
                         json={"batchId": key, "events": [event()]},
                         headers=header)
    assert result.status_code == 200
    first_id = result.json()["requests"][0]["requestId"]
    logged = client.post("/login", data={
        "username": "otheruser", "password": "super-secret-password-2026"
    }, follow_redirects=False)
    assert logged.status_code == 303
    assert client.get("/api/v1/requests").json()["requests"] == []
    csrf2 = client.get("/api/v1/session").json()["csrfToken"]
    header2 = {"X-CSRF-Token": csrf2, "Origin": "https://calendar.example.com"}
    result = client.post("/api/v1/requests/import",
                         json={"batchId": key, "events": [event()]},
                         headers=header2)
    assert result.status_code == 200  # Same external batchId, scoped by user
    assert result.json()["requests"][0]["requestId"] != first_id
    client.post("/login", data={
        "username": "viewer", "password": "super-secret-password-2026"
    }, follow_redirects=False)
    csrf3 = client.get("/api/v1/session").json()["csrfToken"]
    rejected = client.post("/api/v1/requests/import",
                           json={"batchId": "viewertest123", "events": [event()]},
                           headers={"X-CSRF-Token": csrf3})
    assert rejected.status_code == 403


def test_logout_revokes_session(client):
    client.post("/login", data={
        "username": "submitter", "password": "super-secret-password-2026"
    }, follow_redirects=False)
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    logout = client.post("/api/v1/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200
    assert client.get("/api/v1/session").status_code == 401


def test_admin_can_reject_and_disabled_account_loses_session(client):
    client.post("/login", data={
        "username": "admin", "password": "super-secret-password-2026"
    }, follow_redirects=False)
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    headers = {"X-CSRF-Token": csrf}
    key = "batch" + uuid.uuid4().hex
    created = client.post("/api/v1/requests/import", json={
        "batchId": key, "events": [event()]
    }, headers=headers).json()
    rid = created["requests"][0]["requestId"]
    resp = client.post(f"/api/v1/admin/requests/{rid}/reject", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    with db() as conn:
        conn.execute("UPDATE users SET enabled=0 WHERE username='admin'")
    assert client.get("/api/v1/requests").status_code == 401



def test_windows_delete_batch_and_receipt_api(client, monkeypatch):
    from app import main as module

    class Response:
        def execute(self):
            return {"id": "remote-id-1", "etag": '"etag-1"'}

    class FakeEvents:
        def get(self, **kwargs):
            assert kwargs["calendarId"] == "example@example.com"
            return Response()

    class FakeService:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(module, "calendar", lambda: FakeService())
    client.post("/login", data={
        "username": "submitter", "password": "super-secret-password-2026"
    }, follow_redirects=False)
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    headers = {"X-CSRF-Token": csrf, "Origin": "https://calendar.example.com"}
    original = {
        "batchId": "deletebatch123",
        "deletions": [{"eventId": "remote-id-1"}]
    }
    first = client.post("/api/v1/requests/delete-batch", json=original, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["requests"][0]["status"] == "PENDING"
    second = client.post("/api/v1/requests/delete-batch", json=original, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    receipt = client.get("/api/v1/receipts")
    assert receipt.status_code == 200
    row = receipt.json()["requests"][0]
    assert row["batchId"] == "deletebatch123"
    assert row["batchIndex"] == 0
    assert row["requestId"] == first.json()["requests"][0]["requestId"]
    assert row["status"] == "PENDING"
    assert row["googleEventId"] is None
    assert client.post("/api/v1/requests/delete-batch", json={
        "batchId": "deletebatch123",
        "deletions": [{"eventId": "other-event"}]
    }, headers=headers).status_code == 409
