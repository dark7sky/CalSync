import json
from pathlib import Path
from sender.windows.core import Engine

def sample(uid="meeting-1", subject="Test meeting", kind="new"):
    return {
        "uid": uid, "type": kind, "subject": subject,
        "startTime": {"dateTime":"2026-12-01T09:00:00+09:00","timeZone":"Asia/Seoul"},
        "endTime": {"dateTime":"2026-12-01T10:00:00+09:00","timeZone":"Asia/Seoul"},
        "chair": {"displayName":"Example Organizer"}, "location":"Example room",
        "calendarId":"ignored", "attendees":["ignored"], "URL":"https://ignored.invalid",
    }

def write(path: Path, events):
    path.write_text(json.dumps(events), encoding="utf-8")

def test_create_receipt_and_safe_deletion(tmp_path):
    source=tmp_path/"json"; source.mkdir()
    file=source/"events.json"; write(file,[sample()])
    engine=Engine(tmp_path/"private"/"state.db")
    result=engine.scan(source,{"deleted"})
    assert result["events"]==1
    first=engine.prepare()
    assert first==engine.prepare()
    assert set(first)=={"batchId","events"}
    assert "attendees" not in first["events"][0] and "uid" not in first["events"][0]
    assert engine.mark_submitted(first["batchId"])==1
    assert engine.load_receipt(json.dumps({
        "batchId":first["batchId"],"requests":[{"requestId":"req1","status":"PENDING"}]
    }))["updated"]==1
    write(file,[{"uid":"meeting-1","type":"deleted"}])
    scanned=engine.scan(source,{"deleted"})
    assert any("confirmed Google event ID" in e for e in scanned["errors"])
    assert engine.prepare() is None
    done=engine.load_receipt(json.dumps({
        "batchId":first["batchId"],"requests":[
            {"requestId":"req1","status":"SUCCEEDED","googleEventId":"google-event-1"}
        ]}))
    assert done["updated"]==1
    engine.scan(source,{"deleted"})
    deletion=engine.prepare()
    assert deletion["deletions"]==[{"eventId":"google-event-1"}]
    assert engine.mark_submitted(deletion["batchId"])==1
    receipt={"batchId":deletion["batchId"],"requests":[{"requestId":"req2","status":"SUCCEEDED"}]}
    assert engine.load_receipt(json.dumps(receipt))["updated"]==1
    engine.close()

def test_missing_or_invalid_file_never_deletes(tmp_path):
    source=tmp_path/"json";source.mkdir()
    file=source/"a.json";write(file,[sample()])
    engine=Engine(tmp_path/"state.db")
    engine.scan(source,{"cancelled"})
    packet=engine.prepare()
    engine.mark_submitted(packet["batchId"])
    file.write_text("{ bad JSON",encoding="utf-8")
    assert engine.scan(source,{"cancelled"})["errors"]
    file.unlink()
    assert engine.scan(source,{"cancelled"})["events"]==0
    assert engine.prepare() is None
    assert not any(r["action"]=="delete" for r in engine.queue_rows())
    engine.close()

def test_updated_uid_detected_without_delete_or_duplicate_create(tmp_path):
    source=tmp_path/"json";source.mkdir()
    file=source/"a.json";write(file,[sample()])
    engine=Engine(tmp_path/"state.db")
    engine.scan(source)
    write(file,[sample(subject="Changed meeting")])
    assert engine.scan(source)["changes"]==1
    assert len([r for r in engine.queue_rows() if r["action"]=="create"])==1
    engine.close()

def test_browser_export_and_restart(tmp_path):
    source=tmp_path/"json";source.mkdir()
    write(source/"a.json",[sample()])
    db=tmp_path/"state.db"
    engine=Engine(db);engine.scan(source);packet=engine.prepare()
    receipt={"requests":[{
        "batchId":packet["batchId"],"batchIndex":0,"requestId":"req1",
        "status":"SUCCEEDED","googleEventId":"google-uid-1"
    }]}
    assert engine.load_receipt(json.dumps(receipt))["updated"]==1
    engine.close()
    reopened=Engine(db)
    assert reopened.db.execute("SELECT google_id FROM observed").fetchone()[0]=="google-uid-1"
    reopened.close()
