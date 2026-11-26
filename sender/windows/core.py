"""CalSync Windows local sync engine (no network access).

A Menlo-hosted browser can submit batches. The OS client cannot bypass that
network boundary and never silently transmits calendar data. Deletions require
explicit, configured tombstones AND confirmed remote Google event IDs.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

CONTENT_KEYS = ("subject", "startTime", "endTime", "chair", "location")
TERMINAL = {"SUCCEEDED", "REJECTED", "CONFLICT"}
MAX_FILE_BYTES = 5_000_000


def json_compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical(event: dict) -> dict:
    """서버(receiver)가 쓰는 검증 규칙과 동일하게 확인한다.

    서버는 배치 안 이벤트 하나가 규칙을 어기면 요청 전체를 422로 거부하므로,
    스캔 단계에서 미리 걸러 나쁜 이벤트만 errors로 격리해야 한다.
    """
    if not isinstance(event, dict):
        raise ValueError("Each event must be an object")
    subject = event.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("Missing subject")
    if len(subject) > 250:
        raise ValueError("subject must be <= 250 characters")
    location = event.get("location")
    if location is not None and (not isinstance(location, str) or len(location) > 1000):
        raise ValueError("location must be a string <= 1000 characters")
    chair = event.get("chair")
    if chair is not None:
        if not isinstance(chair, dict):
            raise ValueError("chair must be an object")
        for name in ("displayName", "id", "email"):
            value = chair.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > 320):
                raise ValueError("chair." + name + " must be a short string")
    kinds = []
    for key in ("startTime", "endTime"):
        value = event.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"Missing {key} object")
        if value.get("dateTime"):
            kinds.append("timed")
        elif value.get("date"):
            kinds.append("all-day")
        else:
            raise ValueError(f"{key} needs date or dateTime")
    payload = {key: event[key] for key in CONTENT_KEYS if key in event}
    start, end = payload.get("startTime"), payload.get("endTime")
    if kinds[0] != kinds[1]:
        raise ValueError("startTime and endTime must both be all-day or timed")
    if kinds[0] == "timed":
        try:
            first_at = datetime.fromisoformat(str(start["dateTime"]))
            last_at = datetime.fromisoformat(str(end["dateTime"]))
        except ValueError:
            raise ValueError("dateTime must be ISO 8601")
        if (first_at.tzinfo is None) != (last_at.tzinfo is None):
            raise ValueError("startTime and endTime must both include a timezone or neither")
        if last_at <= first_at:
            raise ValueError("endTime must be after startTime")
    if start.get("date") and end.get("date"):
        # 종일 일정: 원본 시스템은 포함형(단일일 = 시작일==종료일)으로 주지만
        # Google/서버는 배타적 종료일을 요구한다. 단일일 표기를 +1일로 변환하고,
        # 종료일<시작일 같은 불량 데이터는 서버 배치 전체 거부(422) 전에 여기서 걸러낸다.
        try:
            first = date.fromisoformat(start["date"])
            last = date.fromisoformat(end["date"])
        except ValueError:
            raise ValueError("All-day date must be YYYY-MM-DD")
        if last < first:
            raise ValueError("All-day endTime is before startTime")
        if last == first:
            payload["endTime"] = dict(end, date=(first + timedelta(days=1)).isoformat())
    return payload


def events_from_file(path: Path) -> list[dict]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"File exceeds {MAX_FILE_BYTES} bytes")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        data = data["events"]
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Expected an event object, array, or {events:[...]}")
    return data


class Engine:
    def __init__(self, state_path: Path):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS observed (
            ref TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            google_id TEXT, state TEXT NOT NULL, last_seen INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT NOT NULL, action TEXT NOT NULL, fingerprint TEXT,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            batch_id TEXT, ordinal INTEGER, request_id TEXT,
            created_at INTEGER NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_queue_batch ON queue(batch_id,ordinal)")
        self.db.commit()

    def close(self):
        self.db.close()

    def scan(self, folder: Path, deletion_types: set[str] | None = None) -> dict:
        """Never infer a delete from a missing/empty/malformed file."""
        deletion_types = {x.strip().lower() for x in (deletion_types or set()) if x.strip()}
        folder = Path(folder)
        if not folder.is_dir():
            raise ValueError("Select an existing folder")
        discovered, errors, seen_refs, changed, replaced = 0, [], set(), 0, 0
        for path in sorted(folder.glob("*.json")):
            try:
                items = events_from_file(path)
            except (ValueError, OSError, UnicodeError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            for i, raw in enumerate(items):
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("Not an event object")
                    if all(raw.get(key) in (None, "", []) for key in
                           ("subject", "startTime", "endTime", "uid", "type")):
                        # Metadata entry (e.g. updateTime-only footer), not an event.
                        continue
                    is_deleted = str(raw.get("type", "")).strip().lower() in deletion_types
                    uid = raw.get("uid")
                    if uid is not None and not isinstance(uid, (str, int)):
                        raise ValueError("uid must be string or integer")
                    # The uid is only for local correlation, never transmitted as
                    # event data. Without uid, content-hash is the stable fallback.
                    if uid is not None and str(uid).strip():
                        ref = f"{path.name}:{uid}"
                    elif is_deleted:
                        raise ValueError("Deletion marker must include uid")
                    else:
                        payload = canonical(raw)
                        ref = "fingerprint:" + hashlib.sha256(
                            json_compact(payload).encode()).hexdigest()
                    if ref in seen_refs:
                        raise ValueError("Duplicate uid/reference across input files")
                    seen_refs.add(ref)
                    discovered += 1
                    old = self.db.execute("SELECT * FROM observed WHERE ref=?", (ref,)).fetchone()
                    active = self.db.execute("""SELECT * FROM queue WHERE ref=?
                        AND status NOT IN ('SUCCEEDED','REJECTED','CONFLICT','cancelled')
                        ORDER BY id DESC LIMIT 1""", (ref,)).fetchone()
                    if is_deleted:
                        if old and old["state"] == "DELETED":
                            continue
                        if old and old["google_id"] and not active:
                            self.db.execute("""INSERT INTO queue(ref,action,fingerprint,payload,created_at)
                                VALUES(?,?,?,?,?)""", (ref, "delete", old["fingerprint"],
                                json_compact({"eventId": old["google_id"]}), int(time.time())))
                        elif not old or not old["google_id"]:
                            errors.append(f"{path.name}[{i}]: deletion requires a confirmed Google event ID")
                        continue
                    payload = canonical(raw)
                    fingerprint = hashlib.sha256(json_compact(payload).encode()).hexdigest()
                    if old is None:
                        self.db.execute("""INSERT INTO observed(ref,fingerprint,state,last_seen)
                            VALUES(?,?,?,?)""", (ref, fingerprint, "NEW", int(time.time())))
                        self.db.execute("""INSERT INTO queue(ref,action,fingerprint,payload,created_at)
                            VALUES(?,?,?,?,?)""", (ref, "create", fingerprint,
                            json_compact(payload), int(time.time())))
                    elif old["fingerprint"] != fingerprint:
                        # 이벤트 수정: 서버가 편집을 지원하지 않으므로 기존 Google 일정
                        # 삭제 요청 + 변경된 내용의 신규 등록 요청으로 대체한다.
                        if old["google_id"]:
                            # Google에 확정된 일정: delete + recreate
                            self.db.execute("""INSERT INTO queue(ref,action,fingerprint,payload,created_at)
                                VALUES(?,?,?,?,?)""", (ref, "delete", old["fingerprint"],
                                json_compact({"eventId": old["google_id"]}), int(time.time())))
                            self.db.execute("""INSERT INTO queue(ref,action,fingerprint,payload,created_at)
                                VALUES(?,?,?,?,?)""", (ref, "create", fingerprint,
                                json_compact(payload), int(time.time())))
                            self.db.execute(
                                "UPDATE observed SET fingerprint=?,last_seen=? WHERE ref=?",
                                (fingerprint, int(time.time()), ref))
                            replaced += 1
                        elif active is not None and active["status"] in ("queued", "prepared"):
                            # 서버 미확정 대기 항목: 내용을 그대로 교체(안전)
                            self.db.execute(
                                "UPDATE queue SET payload=?,fingerprint=? WHERE id=?",
                                (json_compact(payload), fingerprint, active["id"]))
                            self.db.execute(
                                "UPDATE observed SET fingerprint=?,last_seen=? WHERE ref=?",
                                (fingerprint, int(time.time()), ref))
                            replaced += 1
                        elif active is None:
                            # 이전 시도는 종료됐고 확정되지 않음: 새 내용으로 신규 등록
                            self.db.execute("""INSERT INTO queue(ref,action,fingerprint,payload,created_at)
                                VALUES(?,?,?,?,?)""", (ref, "create", fingerprint,
                                json_compact(payload), int(time.time())))
                            self.db.execute(
                                "UPDATE observed SET fingerprint=?,last_seen=? WHERE ref=?",
                                (fingerprint, int(time.time()), ref))
                            replaced += 1
                        else:
                            # 서버 응답 대기 중: 확정되면 다음 스캔에서 delete+recreate
                            changed += 1
                    else:
                        self.db.execute("UPDATE observed SET last_seen=? WHERE ref=?",
                                        (int(time.time()), ref))
                except (ValueError, TypeError) as exc:
                    errors.append(f"{path.name}[{i}]: {exc}")
        self.db.commit()
        return {"events": discovered, "changes": changed, "replaced": replaced,
                "errors": errors,
                "missing_not_deleted": self.db.execute(
                    "SELECT COUNT(*) FROM observed WHERE last_seen<? AND state!='DELETED'",
                    (int(time.time()) - 1,)
                ).fetchone()[0]}

    def queue_rows(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM queue ORDER BY id DESC LIMIT 500")]

    def pending_count(self) -> int:
        return self.db.execute(
            "SELECT count(*) FROM queue WHERE status IN ('queued','prepared','submitted','PENDING','APPROVED','PROCESSING')"
        ).fetchone()[0]

    def queued_count(self) -> int:
        return self.db.execute(
            "SELECT count(*) FROM queue WHERE status='queued'"
        ).fetchone()[0]

    def prepared_count(self) -> int:
        """아직 확정되지 않아 남아 있는 'prepared'(보내기 직전/애매한 실패) 배치 수."""
        return self.db.execute(
            "SELECT count(*) FROM queue WHERE status='prepared'"
        ).fetchone()[0]

    def unconfirmed_count(self) -> int:
        """전송은 됐지만 최종(SUCCEEDED 등) 확정 전인 요청 수."""
        return self.db.execute(
            "SELECT count(*) FROM queue WHERE status IN "
            "('submitted','PENDING','APPROVED','PROCESSING','EXECUTING')"
        ).fetchone()[0]

    def prepare(self) -> dict | None:
        """Generate one persistent batch; repeat gives same batch ID and payload."""
        old = self.db.execute("""SELECT batch_id,action FROM queue
            WHERE status='prepared' AND batch_id IS NOT NULL ORDER BY id LIMIT 1""").fetchone()
        if old:
            bid = old["batch_id"]
        else:
            first = self.db.execute("SELECT action FROM queue WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not first:
                return None
            bid = uuid.uuid4().hex
            rows = self.db.execute(
                "SELECT id FROM queue WHERE status='queued' AND action=? ORDER BY id LIMIT 100",
                (first["action"],)
            ).fetchall()
            for i, row in enumerate(rows):
                self.db.execute("UPDATE queue SET status='prepared',batch_id=?,ordinal=? WHERE id=?",
                                (bid, i, row["id"]))
            self.db.commit()
        rows = self.db.execute("SELECT * FROM queue WHERE batch_id=? ORDER BY ordinal", (bid,)).fetchall()
        if not rows:
            return None
        action = rows[0]["action"]
        return {"batchId": bid, ("events" if action == "create" else "deletions"):
                [json.loads(r["payload"]) for r in rows]}

    def mark_submitted(self, batch_id: str) -> int:
        with self.db:
            cur = self.db.execute(
                "UPDATE queue SET status='submitted' WHERE batch_id=? AND status='prepared'", (batch_id,))
        return cur.rowcount

    def reject_batch(self, batch_id: str, reason: str) -> int:
        """서버가 배치를 명확히 거부(422/409)했을 때 종료 상태로 표시한다.

        'prepared'로 남겨두면 이후 자동 전송이 영구히 막히므로 REJECTED로 닫는다.
        같은 내용을 다시 보내도 결과가 같으므로 재시도하지 않고, 원본 파일이
        수정되어 fingerprint가 바뀌면 다음 스캔에서 다시 등록 대상이 된다.
        """
        with self.db:
            cur = self.db.execute(
                "UPDATE queue SET status='REJECTED' WHERE batch_id=? "
                "AND status IN ('prepared','submitted')", (batch_id,))
            for row in self.db.execute(
                    "SELECT ref,action FROM queue WHERE batch_id=? AND status='REJECTED'",
                    (batch_id,)).fetchall():
                if row["action"] == "create":
                    self.db.execute(
                        "UPDATE observed SET state='REJECTED' "
                        "WHERE ref=? AND google_id IS NULL AND state!='DELETED'", (row["ref"],))
        self.last_reject_reason = reason
        return cur.rowcount

    def load_receipt(self, source: str) -> dict:
        """Accept browser POST receipt or status export, not a claim of sync success."""
        doc = json.loads(source)
        if not isinstance(doc, dict) or not isinstance(doc.get("requests"), list):
            raise ValueError("Expected JSON object containing requests[]")
        count = 0
        errors = []
        with self.db:
            for i, item in enumerate(doc["requests"]):
                if not isinstance(item, dict):
                    errors.append(f"requests[{i}]: invalid object")
                    continue
                batch = item.get("batchId") or doc.get("batchId")
                index = item.get("batchIndex", i if doc.get("batchId") else None)
                if not isinstance(batch, str) or not isinstance(index, int):
                    errors.append(f"requests[{i}]: batchId/batchIndex missing")
                    continue
                row = self.db.execute(
                    "SELECT * FROM queue WHERE batch_id=? AND ordinal=?", (batch, index)).fetchone()
                if not row:
                    errors.append(f"requests[{i}]: no matching local batch")
                    continue
                status = str(item.get("status") or "").upper()
                if row["status"] == "SUCCEEDED" and status != "SUCCEEDED":
                    # A stale/older browser export must not roll back confirmed sync.
                    continue
                if status not in {"PENDING","APPROVED","PROCESSING","EXECUTING","SUCCEEDED",
                                  "FAILED","REJECTED","CONFLICT"}:
                    errors.append(f"requests[{i}]: unknown status")
                    continue
                remote = item.get("googleEventId")
                rid = item.get("requestId")
                if remote is not None and (not isinstance(remote, str) or len(remote) > 1024):
                    errors.append(f"requests[{i}]: invalid googleEventId")
                    continue
                if row["request_id"] and rid and rid != row["request_id"]:
                    errors.append(f"requests[{i}]: request ID mismatch")
                    continue
                self.db.execute(
                    "UPDATE queue SET status=?,request_id=COALESCE(?,request_id) WHERE id=?",
                    (status, rid, row["id"]))
                if status == "SUCCEEDED":
                    if row["action"] == "create":
                        if not remote:
                            errors.append(f"requests[{i}]: SUCCEEDED but missing googleEventId")
                            # Never claim synced if target ID is unknown
                            self.db.execute("UPDATE queue SET status='submitted' WHERE id=?", (row["id"],))
                            continue
                        newer_done = self.db.execute(
                            "SELECT 1 FROM queue WHERE ref=? AND action='create' AND id>? "
                            "AND status='SUCCEEDED' LIMIT 1",
                            (row["ref"], row["id"])).fetchone()
                        if newer_done:
                            # delete+recreate 등으로 더 최신 등록이 이미 확정됐다면
                            # 과거 create 영수증이 매핑을 되돌리지 못하게 한다.
                            count += 1
                            continue
                        self.db.execute(
                            "UPDATE observed SET google_id=?,state='SYNCED' WHERE ref=?",
                            (remote, row["ref"]))
                    elif row["action"] == "delete":
                        newer_create = self.db.execute(
                            "SELECT 1 FROM queue WHERE ref=? AND action='create' AND id>? LIMIT 1",
                            (row["ref"], row["id"])).fetchone()
                        if newer_create:
                            # delete+recreate 흐름: 재생성된 새 일정을 위해 observed를 유지한다.
                            obs = self.db.execute(
                                "SELECT google_id FROM observed WHERE ref=?", (row["ref"],)).fetchone()
                            deleted_id = (json.loads(row["payload"]) or {}).get("eventId")
                            if not (obs and obs["google_id"] and obs["google_id"] != deleted_id):
                                self.db.execute(
                                    "UPDATE observed SET google_id=NULL,state='NEW' WHERE ref=?",
                                    (row["ref"],))
                        else:
                            self.db.execute(
                                "UPDATE observed SET google_id=NULL,state='DELETED' WHERE ref=?",
                                (row["ref"],))
                count += 1
            # 영수증 조회에 성공했는데 서버에 아예 없는 'submitted' 배치를 복구한다.
            # (영수증 조회가 실패한 경우는 이 함수가 호출되지 않으므로 재전송을 남발하지 않는다.)
            requeued: list[str] = []
            if len(doc["requests"]) < 1000:
                seen = {str(item.get("batchId")) for item in doc["requests"]
                        if isinstance(item, dict)}
                cutoff = int(time.time()) - 3 * 86400
                for row in self.db.execute(
                        "SELECT DISTINCT batch_id FROM queue WHERE status='submitted' "
                        "AND batch_id IS NOT NULL AND created_at>=?", (cutoff,)).fetchall():
                    if row["batch_id"] in seen:
                        continue
                    moved = self.db.execute(
                        "UPDATE queue SET status='queued',batch_id=NULL,ordinal=NULL,"
                        "request_id=NULL WHERE batch_id=? AND status='submitted'",
                        (row["batch_id"],)).rowcount
                    if moved:
                        requeued.append(row["batch_id"])
        return {"updated": count, "errors": errors, "requeued": requeued}
