"""CalSync receiver: Menlo 경유 브라우저 제출 후 Google Calendar에 즉시 반영.

실운영 전 .env/Google OAuth 설정과 NPM HTTPS 프록시, 접근 정책 확인이 필요합니다.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app import security
from pydantic import BaseModel, Field

SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
SESSION_COOKIE = "__Host-calsync_session"
MAX_EVENTS_PER_BATCH = 100
app = FastAPI(title="CalSync Receiver", version="0.1.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def setting(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError("Missing required environment variable: " + name)
    return v


def database_path() -> str:
    return os.getenv("CALSYNC_DATABASE", "/data/calsync.db")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@app.on_event("startup")
def startup():
    for key in ("CALSYNC_CALENDAR_ID", "CALSYNC_PUBLIC_ORIGIN"):
        setting(key)
    origin = urlsplit(setting("CALSYNC_PUBLIC_ORIGIN"))
    if origin.scheme != "https" or not origin.netloc or origin.path not in ("", "/"):
        raise RuntimeError("CALSYNC_PUBLIC_ORIGIN must be the public HTTPS origin")
    path = Path(database_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        security.initialize(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            batch_key TEXT,
            batch_index INTEGER,
            operation TEXT NOT NULL,
            payload TEXT NOT NULL,
            event_id TEXT,
            expected_etag TEXT,
            google_event_id TEXT,
            status TEXT NOT NULL,
            actor TEXT NOT NULL,
            actor_id TEXT,
            created_at TEXT NOT NULL,
            last_error TEXT,
            UNIQUE(batch_key, batch_index)
        )""")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
        if "actor_id" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN actor_id TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS rejections (
            id TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            batch_key TEXT,
            error TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            actor TEXT NOT NULL,
            actor_id TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS google_token (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )""")


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none';"
    )
    # Nginx Proxy Manager serves the public HTTPS origin.
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def session(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    with db() as conn:
        data = security.get_session(conn, token)
    if data is None:
        raise HTTPException(401, "Login required or session expired")
    return data


def allowed_origin(origin: str | None) -> bool:
    if not origin or origin.rstrip("/") == setting("CALSYNC_PUBLIC_ORIGIN").rstrip("/"):
        return True
    for entry in os.getenv("CALSYNC_ALLOWED_ORIGINS", "").split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == "null" and origin == "null":
            return True
        if entry.startswith("*."):
            domain = entry[2:]
            if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain):
                continue
            pattern = r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\." + re.escape(domain)
            if re.fullmatch(pattern, origin, flags=re.IGNORECASE):
                return True
        elif re.fullmatch(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)+", entry) and origin.lower() == entry:
            return True
    return False


def require_csrf(request: Request, user: dict = Depends(session),
                 x_csrf_token: str | None = Header(default=None)) -> dict:
    # Cookie SameSite + per-session CSRF token: cross-origin scripts cannot forge POSTs.
    if not x_csrf_token or not hmac.compare_digest(
        x_csrf_token, str(user.get("csrf", ""))
    ):
        raise HTTPException(403, "CSRF token missing or invalid")
    origin = request.headers.get("origin")
    if not allowed_origin(origin):
        raise HTTPException(403, "Unexpected Origin")
    return user


def submitter(user: dict = Depends(require_csrf)) -> dict:
    if user["role"] not in ("submitter", "admin"):
        raise HTTPException(403, "Submit permission required")
    return user


def admin(user: dict = Depends(require_csrf)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


def admin_read(user: dict = Depends(session)) -> dict:
    # 읽기 전용 관리자 API: 상태 변경이 아니므로 CSRF 토큰은 요구하지 않는다
    # (세션 쿠키가 SameSite=Strict로 교차 출처 요청에서 제외된다).
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


def calendar():
    # DB에 저장된 토큰이 우선이고, 없으면 기존 읽기 전용 파일을 사용합니다.
    with db() as conn:
        row = conn.execute("SELECT token_json FROM google_token WHERE id=1").fetchone()
    if row:
        creds = Credentials.from_authorized_user_info(json.loads(row["token_json"]), [SCOPE])
    else:
        token_file = os.getenv("CALSYNC_GOOGLE_TOKEN_FILE", "/run/secrets/google-token.json")
        if not Path(token_file).exists():
            raise HTTPException(503, "Google OAuth token not configured")
        creds = Credentials.from_authorized_user_file(token_file, [SCOPE])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def google_client_config() -> dict:
    path = os.getenv("CALSYNC_GOOGLE_CREDENTIALS_FILE", "/run/secrets/google-credentials.json")
    if not Path(path).exists():
        raise HTTPException(503, "Google OAuth credentials file not configured")
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(503, "Google OAuth credentials file is unreadable")
    client = cfg.get("installed") or cfg.get("web")
    if not client or not client.get("client_id") or not client.get("client_secret"):
        raise HTTPException(503, "Google OAuth credentials file is incomplete")
    return client


def google_redirect_uri() -> str:
    return setting("CALSYNC_PUBLIC_ORIGIN").rstrip("/") + "/oauth/callback"


def record_rejection(endpoint: str, batch_key: str | None, error: str, raw: object,
                     user: dict | None = None) -> None:
    # 디버깅용: 422/409 거부 시 받은 원본을 남깁니다. 기록 실패로 본 처리를 망가뜨리지 않습니다.
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False, default=str)
    try:
        with db() as conn:
            conn.execute("""INSERT INTO rejections
                (id,endpoint,batch_key,error,raw_payload,actor,actor_id,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, endpoint, batch_key, error[:500], raw[:20000],
                 (user or {}).get("username", "anonymous"), (user or {}).get("id"), now_iso()))
    except (sqlite3.Error, OSError):
        pass


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    # 모델 스키마 불일치(422)로 거부된 본문도 원문 그대로 기록합니다.
    try:
        with db() as conn:
            user = security.get_session(conn, request.cookies.get(SESSION_COOKIE))
    except (sqlite3.Error, OSError):
        user = None
    try:
        raw = (await request.body()).decode("utf-8", "replace")
    except Exception:
        raw = "<unreadable body>"
    record_rejection(request.url.path, None, "RequestValidationError",
                     raw, user)
    return JSONResponse(status_code=422,
                        content={"detail": jsonable_encoder(exc.errors())})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_time(raw: object, field: str) -> tuple[dict, datetime | date, bool]:
    if not isinstance(raw, dict):
        raise ValueError(field + " must be an object")
    day = raw.get("date")
    instant = raw.get("dateTime")
    tz = raw.get("timeZone")
    if tz:
        if not isinstance(tz, str):
            raise ValueError(field + ".timeZone must be a string")
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            raise ValueError(field + ".timeZone must be an IANA time zone")
    if instant not in (None, ""):
        if not isinstance(instant, str):
            raise ValueError(field + ".dateTime must be a string")
        try:
            parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(field + ".dateTime must be ISO-8601")
        if parsed.tzinfo is None:
            if not tz:
                raise ValueError(field + ".dateTime needs an offset or timeZone")
            parsed = parsed.replace(tzinfo=ZoneInfo(tz))
        return (
            {"dateTime": parsed.isoformat(), **({"timeZone": tz} if tz else {})},
            parsed.astimezone(timezone.utc), False,
        )
    if day not in (None, ""):
        if not isinstance(day, str):
            raise ValueError(field + ".date must be a string")
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            raise ValueError(field + ".date must be YYYY-MM-DD")
        return {"date": parsed_day.isoformat()}, parsed_day, True
    raise ValueError(field + " requires dateTime or date")


def normalize_event(raw: object) -> dict:
    """Read only the five user-approved fields; ignore type/uid/calendarId/attendees/URL."""
    if not isinstance(raw, dict):
        raise ValueError("Event must be a JSON object")
    subject = raw.get("subject")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 250:
        raise ValueError("subject must be nonempty and <= 250 characters")
    location = raw.get("location")
    if location is not None and (not isinstance(location, str) or len(location) > 1000):
        raise ValueError("location must be a string <= 1000 characters")
    chair = raw.get("chair")
    if chair is not None and not isinstance(chair, dict):
        raise ValueError("chair must be an object")
    chair = chair or {}
    for name in ("displayName", "id", "email"):
        if chair.get(name) is not None and (
            not isinstance(chair[name], str) or len(chair[name]) > 320
        ):
            raise ValueError("chair." + name + " must be a short string")
    start, a, is_all_day = validate_time(raw.get("startTime"), "startTime")
    end, b, end_all_day = validate_time(raw.get("endTime"), "endTime")
    if is_all_day != end_all_day:
        raise ValueError("startTime and endTime must both be all-day or timed")
    if b <= a:
        raise ValueError("endTime must be after startTime (all-day end is exclusive)")
    event = {"summary": subject.strip(), "start": start, "end": end}
    if location:
        event["location"] = location
    # Calendar organizer cannot be set directly. Keep minimal chair display, never invite.
    if chair.get("displayName"):
        event["description"] = "주최: " + chair["displayName"]
    return event


class ImportBatch(BaseModel):
    batchId: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    events: list[dict] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class DeleteRequest(BaseModel):
    eventId: str = Field(min_length=1, max_length=1024)
    # If event list returned an etag, require unchanged event at execution time.
    expectedEtag: str | None = Field(default=None, max_length=512)


class DeleteBatch(BaseModel):
    batchId: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    deletions: list[DeleteRequest] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/privacy")
def privacy_page():
    return FileResponse(Path(__file__).parent / "static" / "privacy.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse("""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>CalSync Login</title><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/static/style.css">
<body><main><section><h1>CalSync 로그인</h1>
<form method="POST" action="/login" autocomplete="on">
<label>사용자 이름 <input name="username" autocomplete="username" required maxlength="64"></label>
<label>비밀번호 <input name="password" type="password" autocomplete="current-password" required></label>
<button class="primary" type="submit">로그인</button>
</form></section></main></body></html>""")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    origin = request.headers.get("origin")
    if not allowed_origin(origin):
        raise HTTPException(403, "Unexpected Origin")
    with db() as conn:
        try:
            user = security.verify_credentials(conn, username, password)
        except ValueError:
            raise HTTPException(429, "Too many login attempts. Retry later")
        if user is None:
            return HTMLResponse("사용자 이름 또는 비밀번호가 올바르지 않습니다.", status_code=401)
        # Rotate credentials on each login; no old browser token survives re-login.
        security.revoke_session(conn, request.cookies.get(SESSION_COOKIE))
        max_age = int(os.getenv("CALSYNC_SESSION_MAX_AGE_SECONDS", "43200"))
        token = security.create_session(conn, user["id"], max_age=max_age)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=True, samesite="strict",
        max_age=max_age, path="/",
    )
    return response


@app.post("/api/v1/logout")
def logout(request: Request, user: dict = Depends(require_csrf)):
    with db() as conn:
        security.revoke_session(conn, request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/")
def home(request: Request):
    try:
        session(request)
    except HTTPException:
        return RedirectResponse("/login")
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/v1/session")
def me(user: dict = Depends(session)):
    return {"role": user["role"], "username": user["username"], "csrfToken": user["csrf"]}


@app.get("/api/v1/events")
def events(user: dict = Depends(session), timeMin: str | None = None, timeMax: str | None = None):
    now = datetime.now(timezone.utc)
    low = timeMin or (now - timedelta(days=30)).isoformat()
    high = timeMax or (now + timedelta(days=90)).isoformat()
    # External client cannot override CALSYNC_CALENDAR_ID.
    params = dict(calendarId=setting("CALSYNC_CALENDAR_ID"), timeMin=low, timeMax=high,
                  singleEvents=True, orderBy="startTime", maxResults=250)
    out = []
    try:
        service = calendar()
        while True:
            response = service.events().list(**params).execute()
            for item in response.get("items", []):
                if item.get("status") == "cancelled":
                    continue
                out.append({
                    "id": item["id"], "summary": item.get("summary", "(제목 없음)"),
                    "start": item.get("start", {}), "end": item.get("end", {}),
                    "location": item.get("location", ""), "etag": item.get("etag"),
                })
            if not response.get("nextPageToken") or len(out) >= 2000:
                break
            params["pageToken"] = response["nextPageToken"]
    except (HttpError, OSError, ValueError) as exc:
        raise HTTPException(502, "Google Calendar listing failed: check token/permissions")
    return {"events": out, "truncated": len(out) >= 2000}


@app.post("/api/v1/requests/import")
def import_events(body: ImportBatch, user: dict = Depends(submitter)):
    normalized = []
    for i, raw in enumerate(body.events):
        try:
            normalized.append(normalize_event(raw))
        except ValueError as e:
            record_rejection("/api/v1/requests/import", user["id"] + ":" + body.batchId,
                             f"events[{i}]: {e}", json.dumps(body.events, ensure_ascii=False), user)
            raise HTTPException(422, f"events[{i}]: {e}")
    results = []
    scoped_batch = user["id"] + ":" + body.batchId
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT batch_index,operation,payload FROM requests WHERE batch_key=? ORDER BY batch_index",
                (scoped_batch,),
            ).fetchall()
            if previous and (len(previous) != len(normalized) or any(
                row["batch_index"] != i or row["operation"] != "create" or row["payload"] != json.dumps(event, ensure_ascii=False, sort_keys=True)
                for i, (row, event) in enumerate(zip(previous, normalized))
            )):
                raise HTTPException(409, "batchId reused with different content")
            for i, event in enumerate(normalized):
                payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                existing = conn.execute(
                    "SELECT id, payload, status FROM requests WHERE batch_key=? AND batch_index=?",
                    (scoped_batch, i),
                ).fetchone()
                if existing:
                    if existing["payload"] != payload:
                        raise HTTPException(409, "batchId reused with different content")
                    results.append({"requestId": existing["id"], "status": existing["status"]})
                    continue
                rid = uuid.uuid4().hex
                google_id = "cs" + uuid.uuid4().hex  # Google ID base32hex-valid chars.
                conn.execute("""INSERT INTO requests
                    (id,batch_key,batch_index,operation,payload,google_event_id,status,actor,actor_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (rid, scoped_batch, i, "create", payload, google_id, "PENDING",
                     user["username"], user["id"], now_iso()))
                results.append({"requestId": rid, "status": "PENDING"})
    except HTTPException as exc:
        if exc.status_code == 409:
            # 트랜잭션 롤백 후 별도 연결로 기록 (지금 연결은 쓰기 잠금을 잡고 있음)
            record_rejection("/api/v1/requests/import", scoped_batch, str(exc.detail),
                             json.dumps(body.events, ensure_ascii=False), user)
        raise
    for result in results:
        if result["status"] in ("PENDING", "FAILED"):
            result["status"] = execute_request(result["requestId"])["status"]
    return {"requests": results, "batchId": body.batchId}


@app.post("/api/v1/requests/delete")
def request_delete(body: DeleteRequest, user: dict = Depends(submitter)):
    # Requires Google event id from the server's currently displayed calendar.
    try:
        existing = calendar().events().get(
            calendarId=setting("CALSYNC_CALENDAR_ID"), eventId=body.eventId
        ).execute()
    except HttpError as exc:
        raise HTTPException(404 if exc.resp.status == 404 else 502,
                            "Cannot verify event on the selected calendar")
    if body.expectedEtag and body.expectedEtag != existing.get("etag"):
        raise HTTPException(409, "Event changed since it was shown")
    rid = uuid.uuid4().hex
    with db() as conn:
        conn.execute("""INSERT INTO requests
            (id,operation,payload,event_id,expected_etag,status,actor,actor_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, "delete", "{}", body.eventId, existing.get("etag"), "PENDING",
             user["username"], user["id"], now_iso()))
    return {"requestId": rid, **execute_request(rid)}


@app.post("/api/v1/requests/delete-batch")
def request_delete_batch(body: DeleteBatch, user: dict = Depends(submitter)):
    scoped_batch = user["id"] + ":" + body.batchId
    payloads = [json.dumps({"eventId": item.eventId, "expectedEtag": item.expectedEtag}, sort_keys=True)
                for item in body.deletions]
    raw_body = {"batchId": body.batchId,
                "deletions": [{"eventId": i.eventId, "expectedEtag": i.expectedEtag} for i in body.deletions]}
    if len({item.eventId for item in body.deletions}) != len(body.deletions):
        record_rejection("/api/v1/requests/delete-batch", scoped_batch,
                         "Duplicate event IDs in delete batch", json.dumps(raw_body, ensure_ascii=False), user)
        raise HTTPException(422, "Duplicate event IDs in delete batch")

    def existing_rows(conn):
        rows = conn.execute("SELECT id,batch_index,operation,payload,status FROM requests WHERE batch_key=? ORDER BY batch_index", (scoped_batch,)).fetchall()
        if rows and (len(rows) != len(payloads) or any(
            row["batch_index"] != i or row["operation"] != "delete" or row["payload"] != payload
            for i, (row, payload) in enumerate(zip(rows, payloads))
        )):
            raise HTTPException(409, "batchId reused with different content")
        return rows

    try:
        with db() as conn:
            previous = existing_rows(conn)
        etags = []
        if not previous:
            svc = calendar()
            for i, item in enumerate(body.deletions):
                try:
                    event = svc.events().get(calendarId=setting("CALSYNC_CALENDAR_ID"), eventId=item.eventId).execute()
                except HttpError as exc:
                    raise HTTPException(404 if exc.resp.status == 404 else 502, f"Cannot verify deletions[{i}]")
                if item.expectedEtag and item.expectedEtag != event.get("etag"):
                    raise HTTPException(409, f"deletions[{i}] was modified")
                etags.append(event.get("etag"))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = existing_rows(conn)
            if not rows:
                for i, (item, payload, etag) in enumerate(zip(body.deletions, payloads, etags)):
                    rid = uuid.uuid4().hex
                    conn.execute("""INSERT INTO requests
                        (id,batch_key,batch_index,operation,payload,event_id,expected_etag,status,actor,actor_id,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (rid, scoped_batch, i, "delete", payload, item.eventId, etag,
                         "PENDING", user["username"], user["id"], now_iso()))
                rows = existing_rows(conn)
    except HTTPException as exc:
        if exc.status_code in (409, 422):
            # 트랜잭션 롤백 후 별도 연결로 기록 (지금 연결은 쓰기 잠금을 잡고 있을 수 있음)
            record_rejection("/api/v1/requests/delete-batch", scoped_batch, str(exc.detail),
                             json.dumps(raw_body, ensure_ascii=False), user)
        raise
    results = [{"requestId": row["id"], "status": row["status"]} for row in rows]
    for result in results:
        if result["status"] in ("PENDING", "FAILED"):
            result["status"] = execute_request(result["requestId"])["status"]
    return {"batchId": body.batchId, "requests": results}


@app.get("/api/v1/receipts")
def export_receipts(user: dict = Depends(session)):
    with db() as conn:
        rows = conn.execute("""SELECT id,batch_key,batch_index,operation,status,google_event_id
            FROM requests WHERE batch_key IS NOT NULL AND actor_id=?
            ORDER BY created_at DESC LIMIT 1000""", (user["id"],)).fetchall()
    return {"requests": [{
        "batchId": row["batch_key"].split(":", 1)[1], "batchIndex": row["batch_index"],
        "requestId": row["id"], "operation": row["operation"], "status": row["status"],
        "googleEventId": row["google_event_id"] if row["operation"] == "create" and row["status"] == "SUCCEEDED" else None,
    } for row in rows]}


@app.get("/api/v1/requests")
def list_requests(user: dict = Depends(session)):
    with db() as conn:
        sql = """SELECT id, operation, status, actor, created_at,
            event_id, google_event_id, last_error, payload FROM requests"""
        if user["role"] == "admin":
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT 200").fetchall()
        else:
            rows = conn.execute(sql + " WHERE actor_id=? ORDER BY created_at DESC LIMIT 200",
                                (user["id"],)).fetchall()
    def formatted(row):
        data = dict(row)
        if user["role"] != "admin":
            data.pop("payload", None)
        return data
    return {"requests": [formatted(row) for row in rows]}


@app.post("/api/v1/admin/requests/{rid}/reject")
def reject(rid: str, user: dict = Depends(admin)):
    with db() as conn:
        res = conn.execute("UPDATE requests SET status='REJECTED' WHERE id=? AND status='PENDING'", (rid,))
        if not res.rowcount:
            raise HTTPException(409, "Request not pending")
    return {"status": "REJECTED"}


@app.post("/api/v1/admin/requests/{rid}/approve")
def approve(rid: str, user: dict = Depends(admin)):
    result = execute_request(rid, strict=True)
    if result["status"] == "FAILED":
        raise HTTPException(502, "Execution failed; retry is possible after checking the request")
    return result


@app.get("/api/v1/admin/rejections")
def list_rejections(user: dict = Depends(admin_read)):
    with db() as conn:
        rows = conn.execute("""SELECT id, created_at, actor, endpoint, batch_key, error, raw_payload
            FROM rejections ORDER BY created_at DESC LIMIT 100""").fetchall()
    return {"rejections": [dict(row) for row in rows]}


@app.get("/api/v1/admin/google/status")
def google_status(user: dict = Depends(admin_read)):
    with db() as conn:
        row = conn.execute("SELECT token_json FROM google_token WHERE id=1").fetchone()
    source = "db" if row else (
        "file" if Path(os.getenv("CALSYNC_GOOGLE_TOKEN_FILE", "/run/secrets/google-token.json")).exists()
        else "none")
    expiry = None
    if row:
        try:
            expiry = json.loads(row["token_json"]).get("expiry")
        except (json.JSONDecodeError, TypeError):
            pass
    return {"source": source, "expiry": expiry, "calendarId": setting("CALSYNC_CALENDAR_ID")}


@app.post("/api/v1/admin/google/reconnect")
def google_reconnect(user: dict = Depends(admin)):
    client = google_client_config()
    state = secrets.token_urlsafe(24)
    now = int(time.time())
    with db() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO oauth_states (state,actor_id,expires_at) VALUES (?,?,?)",
                     (state, user["id"], now + 600))
    params = {
        "client_id": client["client_id"],
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return {"url": GOOGLE_AUTH_URL + "?" + urlencode(params)}


@app.get("/oauth/callback")
def google_callback(state: str = "", code: str = "", error: str = ""):
    back = '<p><a href="/">CalSync로 돌아가기</a></p>'
    if error:
        return HTMLResponse("<p>Google 인증이 취소되었거나 거부되었습니다: "
                            + escape(error) + ".</p>" + back, status_code=400)
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT actor_id, expires_at FROM oauth_states WHERE state=?", (state,)).fetchone()
        if row is not None:
            # 일회용: 확인 즉시 폐기
            conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if row is None or row["expires_at"] < now:
        return HTMLResponse("<p>인증 링크가 만료되었거나 이미 사용되었습니다. "
                            "CalSync에서 다시 시도하세요.</p>" + back, status_code=403)
    client = google_client_config()
    data = urlencode({
        "code": code,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": google_redirect_uri(),
        "grant_type": "authorization_code",
    }).encode()
    request = urllib.request.Request(GOOGLE_TOKEN_URI, data=data)
    try:
        with urllib.request.urlopen(request, timeout=30) as http:
            resp = json.load(http)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("error_description", "")
        except (OSError, json.JSONDecodeError):
            pass
        return HTMLResponse("<p>Google 토큰 교환에 실패했습니다 (HTTP "
                            + escape(str(exc.code)) + ").<br>" + escape(detail) + "</p>" + back,
                            status_code=502)
    except OSError as exc:
        return HTMLResponse("<p>Google 토큰 서버에 연결하지 못했습니다: "
                            + escape(type(exc).__name__) + ".</p>" + back, status_code=502)
    if not resp.get("refresh_token"):
        return HTMLResponse("<p>refresh_token을 받지 못했습니다. 다시 시도하세요.</p>" + back,
                            status_code=502)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(resp["expires_in"]))
    doc = {
        "token": resp["access_token"],
        "refresh_token": resp["refresh_token"],
        "token_uri": GOOGLE_TOKEN_URI,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "scopes": [SCOPE],
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO google_token (id,token_json,updated_at) VALUES (1,?,?)",
                     (json.dumps(doc), now_iso()))
    return HTMLResponse('<meta http-equiv="refresh" content="2; url=/">'
                        "<p>Google 계정 연결에 성공했습니다. 잠시 후 CalSync로 돌아갑니다.</p>" + back)


def execute_request(rid: str, strict: bool = False) -> dict:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Request not found")
        if row["status"] not in ("PENDING", "FAILED"):
            if strict:
                raise HTTPException(409, "Request not pending or retryable")
            return {"status": row["status"]}
        conn.execute("UPDATE requests SET status='PROCESSING',last_error=NULL WHERE id=?", (rid,))
    try:
        svc = calendar()
        if row["operation"] == "create":
            body = json.loads(row["payload"])
            body["id"] = row["google_event_id"]
            body["extendedProperties"] = {"private": {"calsyncRequestId": rid}}
            try:
                svc.events().insert(
                    calendarId=setting("CALSYNC_CALENDAR_ID"), body=body,
                    sendUpdates="none"
                ).execute()
            except HttpError as exc:
                if exc.resp.status != 409:
                    raise
                # Deterministic Google event ID enables safe retry after uncertain insert.
                found = svc.events().get(
                    calendarId=setting("CALSYNC_CALENDAR_ID"),
                    eventId=row["google_event_id"],
                ).execute()
                if found.get("extendedProperties", {}).get("private", {}).get("calsyncRequestId") != rid:
                    raise RuntimeError("Google event ID conflict")
        elif row["operation"] == "delete":
            try:
                current = svc.events().get(
                    calendarId=setting("CALSYNC_CALENDAR_ID"), eventId=row["event_id"],
                ).execute()
            except HttpError as exc:
                if exc.resp.status not in (404, 410):
                    raise
                current = None
            if current and current.get("status") == "cancelled":
                current = None
            if current:
                if row["expected_etag"] and row["expected_etag"] != current.get("etag"):
                    with db() as conn:
                        conn.execute(
                            "UPDATE requests SET status='CONFLICT',last_error=? WHERE id=?",
                            ("Event changed since request; no deletion performed", rid),
                        )
                    return {"status": "CONFLICT"}
                deletion = svc.events().delete(
                    calendarId=setting("CALSYNC_CALENDAR_ID"), eventId=row["event_id"],
                    sendUpdates="none",
                )
                if row["expected_etag"]:
                    deletion.headers["If-Match"] = row["expected_etag"]
                try:
                    deletion.execute()
                except HttpError as exc:
                    if exc.resp.status != 412:
                        raise
                    with db() as conn:
                        conn.execute("UPDATE requests SET status='CONFLICT',last_error=? WHERE id=?",
                                     ("Event changed since request; no deletion performed", rid))
                    return {"status": "CONFLICT"}
        else:
            raise RuntimeError("Unknown operation")
        with db() as conn:
            conn.execute("UPDATE requests SET status='SUCCEEDED' WHERE id=?", (rid,))
        return {"status": "SUCCEEDED"}
    except (HttpError, Exception) as exc:
        error = ("Google Calendar HTTP " + str(exc.resp.status)
                 if isinstance(exc, HttpError) else type(exc).__name__)
        with db() as conn:
            conn.execute(
                "UPDATE requests SET status='FAILED',last_error=? WHERE id=?",
                (error, rid),
            )
        return {"status": "FAILED"}
