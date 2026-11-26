import json
import urllib.parse
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

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
def fake_calendar(monkeypatch):
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = {"etag": "etag-1"}
    service.events.return_value.delete.return_value.headers = {}
    monkeypatch.setattr("app.main.calendar", lambda: service)
    return service


@pytest.fixture
def client(tmp_path, monkeypatch, fake_calendar):
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
    assert first.json()["requests"][0]["status"] == "SUCCEEDED"
    again = client.post("/api/v1/requests/import", json=payload, headers=headers)
    assert again.status_code == 200
    assert first.json()["requests"] == again.json()["requests"]
    changed = {"batchId": key, "events": [event(subject="다른 제목")]}
    conflict = client.post("/api/v1/requests/import", json=changed, headers=headers)
    assert conflict.status_code == 409


def test_import_executes_once_and_retries_failure(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    payload = {"batchId": "retry-test-001", "events": [event()]}
    execute = fake_calendar.events.return_value.insert.return_value.execute
    execute.side_effect = [OSError("temporary"), {}]
    first = client.post("/api/v1/requests/import", json=payload, headers=headers)
    assert first.json()["requests"][0]["status"] == "FAILED"
    second = client.post("/api/v1/requests/import", json=payload, headers=headers)
    assert second.json()["requests"][0]["status"] == "SUCCEEDED"
    client.post("/api/v1/requests/import", json=payload, headers=headers)
    assert execute.call_count == 2


def test_delete_executes_immediately_with_etag(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    result = client.post("/api/v1/requests/delete", json={"eventId": "event123", "expectedEtag": "etag-1"}, headers=headers)
    assert result.json()["status"] == "SUCCEEDED"
    assert fake_calendar.events.return_value.delete.return_value.headers["If-Match"] == "etag-1"
    fake_calendar.events.return_value.delete.return_value.execute.assert_called_once()


def test_delete_rejects_changed_event_and_conditional_conflict(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    route = "/api/v1/requests/delete"
    assert client.post(route, json={"eventId": "event123", "expectedEtag": "stale"}, headers=headers).status_code == 409
    fake_calendar.events.return_value.delete.return_value.execute.side_effect = HttpError(
        SimpleNamespace(status=412, reason="Precondition Failed"), b"{}"
    )
    result = client.post(route, json={"eventId": "event123", "expectedEtag": "etag-1"}, headers=headers)
    assert result.json()["status"] == "CONFLICT"


def test_batch_partial_failure_and_retry_only_failed(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    payload = {"batchId": "batch-partial-001", "events": [event(), event(subject="두번째")]}
    execute = fake_calendar.events.return_value.insert.return_value.execute
    execute.side_effect = [{}, OSError("temporary"), {}]
    first = client.post("/api/v1/requests/import", json=payload, headers=headers).json()
    assert [r["status"] for r in first["requests"]] == ["SUCCEEDED", "FAILED"]
    second = client.post("/api/v1/requests/import", json=payload, headers=headers).json()
    assert [r["status"] for r in second["requests"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert execute.call_count == 3


def test_sender_receipt_and_idempotent_delete_batch(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    created = client.post("/api/v1/requests/import", json={"batchId": "sender-create-01", "events": [event()]}, headers=headers).json()
    assert created["requests"][0]["status"] == "SUCCEEDED"
    receipt = client.get("/api/v1/receipts").json()["requests"][0]
    assert (receipt["batchId"], receipt["batchIndex"], receipt["operation"], receipt["status"]) == ("sender-create-01", 0, "create", "SUCCEEDED")
    assert receipt["googleEventId"]
    packet = {"batchId": "sender-delete-01", "deletions": [{"eventId": "event123"}]}
    first = client.post("/api/v1/requests/delete-batch", json=packet, headers=headers)
    assert first.status_code == 200 and first.json()["requests"][0]["status"] == "SUCCEEDED"
    # The target no longer exists when a lost response is retried.
    fake_calendar.events.return_value.get.return_value.execute.side_effect = HttpError(SimpleNamespace(status=404, reason="Not found"), b"{}")
    again = client.post("/api/v1/requests/delete-batch", json=packet, headers=headers)
    assert again.status_code == 200 and again.json() == first.json()
    fake_calendar.events.return_value.delete.return_value.execute.assert_called_once()
    receipt = {r["batchId"]: r for r in client.get("/api/v1/receipts").json()["requests"]}
    assert receipt["sender-delete-01"]["status"] == "SUCCEEDED"
    assert receipt["sender-delete-01"]["googleEventId"] is None


def test_delete_batch_conflict_and_receipt_user_isolation(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    packet = {"batchId": "delete-isolated-1", "deletions": [{"eventId": "event123", "expectedEtag": "etag-1"}]}
    assert client.post("/api/v1/requests/delete-batch", json=packet, headers=headers).status_code == 200
    packet["deletions"][0]["eventId"] = "different"
    assert client.post("/api/v1/requests/delete-batch", json=packet, headers=headers).status_code == 409
    client.post("/login", data={"username": "otheruser", "password": "super-secret-password-2026"})
    assert client.get("/api/v1/receipts").json()["requests"] == []


def test_delete_batch_validation_and_cross_operation_idempotency(client, fake_calendar):
    assert client.get("/api/v1/receipts").status_code == 401
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    packet = {"batchId": "delete-validate-1", "deletions": [{"eventId": "event123", "expectedEtag": "stale"}]}
    assert client.post("/api/v1/requests/delete-batch", json=packet, headers=headers).status_code == 409
    assert client.get("/api/v1/receipts").json()["requests"] == []
    packet["deletions"][0]["expectedEtag"] = "etag-1"
    assert client.post("/api/v1/requests/delete-batch", json=packet, headers=headers).status_code == 200
    packet["deletions"].append({"eventId": "event456"})
    assert client.post("/api/v1/requests/delete-batch", json=packet, headers=headers).status_code == 409
    assert client.post("/api/v1/requests/import", json={"batchId": "delete-validate-1", "events": [event()]}, headers=headers).status_code == 409


def test_receipt_failed_create_hides_google_id(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    fake_calendar.events.return_value.insert.return_value.execute.side_effect = OSError("temporary")
    client.post("/api/v1/requests/import", json={"batchId": "failed-receipt-1", "events": [event()]}, headers=headers)
    row = client.get("/api/v1/receipts").json()["requests"][0]
    assert row["status"] == "FAILED" and row["googleEventId"] is None


def test_batch_id_cannot_be_reused_with_different_length(client, fake_calendar):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    payload = {"batchId": "batch-length-001", "events": [event()]}
    assert client.post("/api/v1/requests/import", json=payload, headers=headers).status_code == 200
    payload["events"].append(event(subject="추가"))
    assert client.post("/api/v1/requests/import", json=payload, headers=headers).status_code == 409
    assert fake_calendar.events.return_value.insert.return_value.execute.call_count == 1


def test_csrf_and_admin_required(client):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"}, follow_redirects=False)
    key = "batch" + uuid.uuid4().hex
    payload = {"batchId": key, "events": [event()]}
    assert client.post("/api/v1/requests/import", json=payload).status_code == 403
    csrf = client.get("/api/v1/session").json()["csrfToken"]
    headers = {"X-CSRF-Token": csrf, "Origin": "https://calendar.example.com"}
    rid = client.post("/api/v1/requests/import", json=payload, headers=headers).json()["requests"][0]["requestId"]
    assert client.post(f"/api/v1/admin/requests/{rid}/reject", headers=headers).status_code == 403


@pytest.mark.parametrize("origin,allowed", [
    ("https://safe.menlosecurity.com", True),
    ("https://abc-menlo-view.menlosecurity.com", True),
    ("https://another.menlosecurity.com", True),
    ("https://menlosecurity.com", False),
    ("http://abc-menlo-view.menlosecurity.com", False),
    ("https://abc-menlo-view.menlosecurity.com.evil.example", False),
    ("https://evil.abc-menlo-view.menlosecurity.com", False),
    ("https://abc-menlo-view.menlosecurity.com:444", False),
    ("https://safe.menlosecurity.com/path", False),
])
def test_menlo_origin_login_and_csrf(client, monkeypatch, origin, allowed):
    monkeypatch.setenv("CALSYNC_ALLOWED_ORIGINS", "*.menlosecurity.com")
    login = client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"}, headers={"Origin": origin}, follow_redirects=False)
    assert login.status_code == (303 if allowed else 403)
    if allowed:
        token = client.get("/api/v1/session").json()["csrfToken"]
        assert client.post("/api/v1/logout", headers={"Origin": origin}).status_code == 403
        assert client.post("/api/v1/logout", headers={"Origin": origin, "X-CSRF-Token": token}).status_code == 200


def test_extra_origins_are_opt_in(client, monkeypatch):
    monkeypatch.delenv("CALSYNC_ALLOWED_ORIGINS", raising=False)
    result = client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"}, headers={"Origin": "https://safe.menlosecurity.com"})
    assert result.status_code == 403


def test_exact_allowed_origin(client, monkeypatch):
    monkeypatch.setenv("CALSYNC_ALLOWED_ORIGINS", "https://safe.menlosecurity.com")
    data = {"username": "submitter", "password": "super-secret-password-2026"}
    assert client.post("/login", data=data, headers={"Origin": "https://safe.menlosecurity.com"}, follow_redirects=False).status_code == 303
    assert client.post("/login", data=data, headers={"Origin": "https://other.menlosecurity.com"}).status_code == 403


def test_null_origin_requires_explicit_allow_and_csrf(client, monkeypatch):
    data = {"username": "submitter", "password": "super-secret-password-2026"}
    monkeypatch.delenv("CALSYNC_ALLOWED_ORIGINS", raising=False)
    assert client.post("/login", data=data, headers={"Origin": "null"}).status_code == 403
    monkeypatch.setenv("CALSYNC_ALLOWED_ORIGINS", "*.menlosecurity.com,null")
    assert client.post("/login", data=data, headers={"Origin": "null"}, follow_redirects=False).status_code == 303
    token = client.get("/api/v1/session").json()["csrfToken"]
    assert client.post("/api/v1/logout", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/v1/logout", headers={"Origin": "null", "X-CSRF-Token": token}).status_code == 200



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
    with db() as conn:
        conn.execute("UPDATE requests SET status='PENDING' WHERE id=?", (rid,))
    resp = client.post(f"/api/v1/admin/requests/{rid}/reject", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    with db() as conn:
        conn.execute("UPDATE users SET enabled=0 WHERE username='admin'")
    assert client.get("/api/v1/requests").status_code == 401


def test_rejected_import_payload_is_recorded_for_admin_debug(client):
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    bad = {"batchId": "reject-normalize-1", "events": [event(subject="")]}
    assert client.post("/api/v1/requests/import", json=bad, headers=headers).status_code == 422
    # 스키마 불일치(누락 필드) 422도 기록된다
    assert client.post("/api/v1/requests/import",
                       json={"batchId": "reject-schema-1"}, headers=headers).status_code == 422
    # batchId 재사용 충돌(409)도 기록된다
    ok = {"batchId": "reject-conflict-1", "events": [event()]}
    assert client.post("/api/v1/requests/import", json=ok, headers=headers).status_code == 200
    ok["events"][0]["subject"] = "다른 제목"
    assert client.post("/api/v1/requests/import", json=ok, headers=headers).status_code == 409
    # 거부 기록은 관리자만 볼 수 있다
    assert client.get("/api/v1/admin/rejections").status_code == 403
    client.post("/login", data={"username": "admin", "password": "super-secret-password-2026"})
    rows = client.get("/api/v1/admin/rejections").json()["rejections"]
    assert len(rows) == 3
    by_error = {r["error"]: r for r in rows}
    assert any(e.startswith("events[0]") for e in by_error)
    assert any("batchId reused" in e for e in by_error)
    recorded = next(r for r in rows if r["error"].startswith("events[0]"))
    assert json.loads(recorded["raw_payload"])[0]["subject"] == ""
    assert recorded["actor"] == "submitter"
    assert recorded["endpoint"] == "/api/v1/requests/import"


def test_google_reconnect_flow(client, tmp_path, monkeypatch):
    creds = {"installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-secret",
    }}
    cred_path = tmp_path / "credentials.json"
    cred_path.write_text(json.dumps(creds), encoding="utf-8")
    monkeypatch.setenv("CALSYNC_GOOGLE_CREDENTIALS_FILE", str(cred_path))

    class FakeTokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "access_token": "fake-access", "refresh_token": "fake-refresh",
                "expires_in": 3600, "token_type": "Bearer",
            }).encode()

    monkeypatch.setattr("app.main.urllib.request.urlopen",
                        lambda request, timeout=30: FakeTokenResponse())

    client.post("/login", data={"username": "admin", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    assert client.get("/api/v1/admin/google/status", headers=headers).json()["source"] in ("file", "none")

    denied = client.post("/api/v1/admin/google/reconnect", headers=headers)
    assert denied.status_code == 200
    url = denied.json()["url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url and "prompt=consent" in url
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    assert urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0] \
        == "https://calendar.example.com/oauth/callback"

    ok = client.get("/oauth/callback?state=" + state + "&code=fake-code")
    assert ok.status_code == 200
    status = client.get("/api/v1/admin/google/status", headers=headers).json()
    assert status["source"] == "db" and status["expiry"]

    # state는 일회용이다
    replay = client.get("/oauth/callback?state=" + state + "&code=again")
    assert replay.status_code == 403
    # 만료/존재하지 않는 state도 403이다
    assert client.get("/oauth/callback?state=unknown&code=x").status_code == 403

    # 콜백 페이지는 외부 스크립트 없이 meta refresh로 돌아간다
    assert "http-equiv" in ok.text
    assert "location.replace" not in ok.text


def test_google_reconnect_requires_admin(client, tmp_path, monkeypatch):
    creds = {"installed": {"client_id": "cid", "client_secret": "sec"}}
    cred_path = tmp_path / "credentials.json"
    cred_path.write_text(json.dumps(creds), encoding="utf-8")
    monkeypatch.setenv("CALSYNC_GOOGLE_CREDENTIALS_FILE", str(cred_path))
    client.post("/login", data={"username": "submitter", "password": "super-secret-password-2026"})
    headers = {"X-CSRF-Token": client.get("/api/v1/session").json()["csrfToken"]}
    assert client.post("/api/v1/admin/google/reconnect", headers=headers).status_code == 403
    assert client.get("/api/v1/admin/google/status", headers=headers).status_code == 403


def test_windows_delete_batch_and_receipt_api(client, monkeypatch):
    from app import main as module

    class Response:
        def execute(self):
            return {"id": "remote-id-1", "etag": '"etag-1"'}

    class FakeEvents:
        def get(self, **kwargs):
            assert kwargs["calendarId"] == "example@example.com"
            return Response()

        def delete(self, **kwargs):
            assert kwargs["calendarId"] == "example@example.com"
            assert kwargs["eventId"] == "remote-id-1"

            class Deletion:
                headers = {}

                def execute(self):
                    return {}

            return Deletion()

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
    assert first.json()["requests"][0]["status"] == "SUCCEEDED"
    second = client.post("/api/v1/requests/delete-batch", json=original, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    receipt = client.get("/api/v1/receipts")
    assert receipt.status_code == 200
    row = receipt.json()["requests"][0]
    assert row["batchId"] == "deletebatch123"
    assert row["batchIndex"] == 0
    assert row["requestId"] == first.json()["requests"][0]["requestId"]
    assert row["status"] == "SUCCEEDED"
    assert row["googleEventId"] is None
    assert client.post("/api/v1/requests/delete-batch", json={
        "batchId": "deletebatch123",
        "deletions": [{"eventId": "other-event"}]
    }, headers=headers).status_code == 409
