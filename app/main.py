"""CalSync receiver MVP: Menlo 경유 브라우저 제출 + 승인 후 Google Calendar 반영.

실운영 전 .env/Google OAuth 설정과 NPM HTTPS 프록시, 접근 정책 확인이 필요합니다.
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app import security
from pydantic import BaseModel, Field

SCOPE = "https://www.googleapis.com/auth/calendar.events"
SESSION_COOKIE = "calsync_session"
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


def require_csrf(request: Request, user: dict = Depends(session),
                 x_csrf_token: str | None = Header(default=None)) -> dict:
    # Cookie SameSite + per-session CSRF token: cross-origin scripts cannot forge POSTs.
    if not x_csrf_token or not hmac.compare_digest(
        x_csrf_token, str(user.get("csrf", ""))
    ):
        raise HTTPException(403, "CSRF token missing or invalid")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != setting("CALSYNC_PUBLIC_ORIGIN").rstrip("/"):
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


def calendar():
    token_file = os.getenv("CALSYNC_GOOGLE_TOKEN_FILE", "/run/secrets/google-token.json")
    if not Path(token_file).exists():
        raise HTTPException(503, "Google OAuth token not configured")
    creds = Credentials.from_authorized_user_file(token_file, [SCOPE])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


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


@app.get("/healthz")
def health():
    return {"status": "ok"}


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
    if origin and origin.rstrip("/") != setting("CALSYNC_PUBLIC_ORIGIN").rstrip("/"):
        raise HTTPException(403, "Unexpected Origin")
    with db() as conn:
        try:
            user = security.verify_credentials(conn, username, password)
        except ValueError:
            raise HTTPException(429, "Too many login attempts. Retry later")
        if user is None:
            return HTMLResponse("사용자 이름 또는 비밀번호가 올바르지 않습니다.", status_code=401)
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
            raise HTTPException(422, f"events[{i}]: {e}")
    results = []
    scoped_batch = user["id"] + ":" + body.batchId
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
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
    return {"requestId": rid, "status": "PENDING"}


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
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Request not found")
        if row["status"] not in ("PENDING", "FAILED"):
            raise HTTPException(409, "Request not pending or retryable")
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
                if exc.resp.status != 404:
                    raise
                current = None
            if current:
                if row["expected_etag"] and row["expected_etag"] != current.get("etag"):
                    with db() as conn:
                        conn.execute(
                            "UPDATE requests SET status='CONFLICT',last_error=? WHERE id=?",
                            ("Event changed since request; no deletion performed", rid),
                        )
                    return {"status": "CONFLICT"}
                svc.events().delete(
                    calendarId=setting("CALSYNC_CALENDAR_ID"), eventId=row["event_id"],
                    sendUpdates="none",
                ).execute()
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
        raise HTTPException(502, "Execution failed; retry is possible after checking the request")
