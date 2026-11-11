import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, normalize_event


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
    monkeypatch.setenv("CALSYNC_SESSION_SECRET", "a" * 48)
    monkeypatch.setenv("CALSYNC_SUBMITTER_PASSWORD", "submitter-secret")
    monkeypatch.setenv("CALSYNC_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("CALSYNC_DATABASE", str(tmp_path / "events.db"))
    with TestClient(app, base_url="https://calendar.example.com") as c:
        yield c


def test_authenticated_browser_import_and_idempotency(client):
    assert client.get("/api/v1/requests").status_code == 401
    response = client.post("/login", data={"password": "submitter-secret"}, follow_redirects=False)
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
    client.post("/login", data={"password": "submitter-secret"}, follow_redirects=False)
    key = "batch" + uuid.uuid4().hex
    payload = {"batchId": key, "events": [event()]}
    assert client.post("/api/v1/requests/import", json=payload).status_code == 403
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    headers = {"X-CSRF-Token": csrf, "Origin": "https://calendar.example.com"}
    rid = client.post("/api/v1/requests/import", json=payload, headers=headers).json()["requests"][0]["requestId"]
    assert client.post(f"/api/v1/admin/requests/{rid}/reject", headers=headers).status_code == 403
