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

def test_updated_uid_replaces_pending_create_without_duplicate(tmp_path):
    source=tmp_path/"json";source.mkdir()
    file=source/"a.json";write(file,[sample()])
    engine=Engine(tmp_path/"state.db")
    engine.scan(source)
    write(file,[sample(subject="Changed meeting")])
    result=engine.scan(source)
    # 아직 서버 확정 전인 대기 생성 요청은 내용을 그대로 교체한다(중복 create 없음).
    assert result["replaced"]==1
    creates=[r for r in engine.queue_rows() if r["action"]=="create"]
    assert len(creates)==1
    assert "Changed meeting" in creates[0]["payload"]
    assert not any(r["action"]=="delete" for r in engine.queue_rows())
    engine.close()

def test_modified_synced_event_queues_delete_and_recreate(tmp_path):
    source=tmp_path/"json";source.mkdir()
    file=source/"a.json";write(file,[sample()])
    engine=Engine(tmp_path/"state.db")
    engine.scan(source);packet=engine.prepare()
    engine.mark_submitted(packet["batchId"])
    engine.load_receipt(json.dumps({"requests":[
        {"batchId":packet["batchId"],"batchIndex":0,"requestId":"req1",
         "status":"SUCCEEDED","googleEventId":"g-1"}]}))
    write(file,[sample(subject="Moved meeting")])
    result=engine.scan(source)
    # Google에 확정된 일정이 수정되면 기존 삭제 + 변경 내용 신규 등록으로 반영한다.
    assert result["replaced"]==1
    rows=engine.queue_rows()
    assert any(r["action"]=="delete" and json.loads(r["payload"])=={"eventId":"g-1"}
               for r in rows)
    creates=[r for r in rows if r["action"]=="create" and "Moved meeting" in r["payload"]]
    assert len(creates)==1 and creates[0]["status"]=="queued"
    engine.close()

def test_metadata_entries_skipped_silently(tmp_path):
    source=tmp_path/"json";source.mkdir()
    write(source/"s.json",[sample(),{"updateTime":"2026-10-28 19:17:24.045278"}])
    write(source/"t.json",{"오늘":{"입문":202610280840},"월종합":{"평일근무":9036}})
    engine=Engine(tmp_path/"state.db")
    result=engine.scan(source,set())
    assert result["events"]==1
    assert result["errors"]==[]
    packet=engine.prepare()
    assert len(packet["events"])==1
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
