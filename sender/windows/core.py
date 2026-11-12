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
from pathlib import Path

CONTENT_KEYS = ("subject", "startTime", "endTime", "chair", "location")
TERMINAL = {"SUCCEEDED", "REJECTED", "CONFLICT"}
MAX_FILE_BYTES = 5_000_000


def json_compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Each event must be an object")
    if not isinstance(event.get("subject"), str) or not event["subject"].strip():
        raise ValueError("Missing subject")
    for key in ("startTime", "endTime"):
        if not isinstance(event.get(key), dict):
            raise ValueError(f"Missing {key} object")
        if not event[key].get("dateTime") and not event[key].get("date"):
            raise ValueError(f"{key} needs date or dateTime")
    return {key: event[key] for key in CONTENT_KEYS if key in event}


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
        discovered, errors, seen_refs, changed = 0, [], set(), 0
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
                        # Avoid corrupting the original mapping. Event editing isn't
                        # supported by receiver; report it without delete+recreate.
                        changed += 1
                    else:
                        self.db.execute("UPDATE observed SET last_seen=? WHERE ref=?",
                                        (int(time.time()), ref))
                except (ValueError, TypeError) as exc:
                    errors.append(f"{path.name}[{i}]: {exc}")
        self.db.commit()
        return {"events": discovered, "changes": changed, "errors": errors,
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
                        self.db.execute(
                            "UPDATE observed SET google_id=?,state='SYNCED' WHERE ref=?",
                            (remote, row["ref"]))
                    elif row["action"] == "delete":
                        self.db.execute(
                            "UPDATE observed SET google_id=NULL,state='DELETED' WHERE ref=?",
                            (row["ref"],))
                count += 1
        return {"updated": count, "errors": errors}
