
"use strict";
let csrf = "";
let role = "";
let pendingBatchId = null;
let pendingSingle = null;

const el = id => document.getElementById(id);
const text = (parent, tag, value, className="") => {
  const node = document.createElement(tag);
  node.textContent = value == null ? "" : String(value);
  if (className) node.className = className;
  parent.appendChild(node);
  return node;
};
function message(msg, type="") {
  el("message").textContent = msg;
  el("message").className = "message " + type;
}
async function call(path, options={}) {
  const method = options.method || "GET";
  const headers = {...(options.headers || {})};
  if (method !== "GET") {
    headers["X-CSRF-Token"] = csrf;
    if (options.body) headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    credentials:"same-origin", cache:"no-store", ...options, headers
  });
  if (response.status === 401) { location.href = "/login"; throw new Error("로그인이 필요합니다"); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(
    (typeof data.detail === "string" ? data.detail : "HTTP " + response.status)
  );
  return data;
}
function fmtTime(t) {
  return (t && (t.dateTime || t.date)) || "";
}
function button(parent, label, action) {
  const b = text(parent, "button", label);
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await action(); } catch(e) { message(e.message, "error"); }
    finally { b.disabled = false; }
  });
  return b;
}
async function refreshEvents() {
  const holder = el("events");
  holder.replaceChildren();
  try {
    const data = await call("/api/v1/events");
    for (const e of data.events) {
      const item = document.createElement("div");
      item.className = "event";
      text(item, "strong", e.summary);
      text(item, "div", fmtTime(e.start) + " ~ " + fmtTime(e.end));
      if (e.location) text(item, "div", e.location, "muted");
      if (role !== "viewer") button(item, "삭제", async () => {
        if (!confirm("이 일정을 Google Calendar에서 삭제할까요? 즉시 반영됩니다.")) return;
        const result = await call("/api/v1/requests/delete", {
          method:"POST", body: JSON.stringify({eventId:e.id,expectedEtag:e.etag})
        });
        if (result.status !== "SUCCEEDED") throw new Error("삭제 처리 상태: " + result.status);
        message("일정을 삭제했습니다.", "success");
        await refreshRequests();
        await refreshEvents();
      });
      holder.appendChild(item);
    }
    if (data.events.length === 0) text(holder, "p", "표시할 일정이 없습니다.", "muted");
    if (data.truncated) text(holder, "p", "결과 일부만 표시됩니다.", "muted");
  } catch(e) {
    text(holder, "p", "일정 조회 실패: " + e.message, "muted");
  }
}
async function refreshRequests() {
  const holder = el("requests");
  holder.replaceChildren();
  try {
    const data = await call("/api/v1/requests");
    for (const r of data.requests) {
      const item = document.createElement("div");
      item.className = "request";
      text(item, "strong", r.operation.toUpperCase() + " · " + r.status);
      text(item, "div", r.created_at + " | " + r.id, "muted small");
      if (r.last_error) text(item, "div", r.last_error, "error");
      if (role === "admin" && r.status === "PENDING") {
        button(item, "승인/실행", async () => {
          if (!confirm("Google Calendar에 이 요청을 실제 반영할까요?")) return;
          await call("/api/v1/admin/requests/" + r.id + "/approve", {method:"POST"});
          message("승인되어 실행됐습니다.", "success");
          await refreshRequests();
          await refreshEvents();
        });
        button(item, "거절", async () => {
          await call("/api/v1/admin/requests/" + r.id + "/reject", {method:"POST"});
          await refreshRequests();
        });
      }
      if (role === "admin" && r.status === "FAILED") {
        button(item, "실패건 재시도", async () => {
          if (!confirm("실패 원인을 확인했습니까? 동일 Google 이벤트 ID로 재시도합니다.")) return;
          await call("/api/v1/admin/requests/" + r.id + "/approve", {method:"POST"});
          await refreshRequests();
        });
      }
      holder.appendChild(item);
    }
    if (data.requests.length === 0) text(holder, "p", "접수된 요청이 없습니다.", "muted");
  } catch(e) { message("요청 조회 실패: " + e.message, "error"); }
}
async function refreshGoogleStatus() {
  try {
    const d = await call("/api/v1/admin/google/status");
    const exp = d.expiry
      ? new Date(d.expiry).toLocaleString("ko-KR", {timeZone: "Asia/Seoul"})
      : "알 수 없음";
    const src = {db: "서버 DB", file: "파일(기존 방식)", none: "미설정"}[d.source] || d.source;
    el("googleStatus").textContent =
      "토큰 저장: " + src + " · access token 만료: " + exp + " · 대상 캘린더: " + d.calendarId;
  } catch(e) { el("googleStatus").textContent = "상태 확인 실패: " + e.message; }
}
async function refreshRejections() {
  const holder = el("rejections");
  holder.replaceChildren();
  try {
    const d = await call("/api/v1/admin/rejections");
    for (const r of d.rejections) {
      const item = document.createElement("div");
      item.className = "request";
      text(item, "strong", r.created_at.slice(0, 19) + " · " + r.actor + " · " + r.endpoint);
      text(item, "div", r.error, "error");
      const det = document.createElement("details");
      const summ = document.createElement("summary");
      summ.textContent = "받은 원본 보기";
      const pre = document.createElement("pre");
      pre.className = "raw";
      pre.textContent = r.raw_payload;
      det.appendChild(summ);
      det.appendChild(pre);
      item.appendChild(det);
      holder.appendChild(item);
    }
    if (!d.rejections.length) text(holder, "p", "거부된 요청이 없습니다.", "muted");
  } catch(e) { message("거부 기록 조회 실패: " + e.message, "error"); }
}
function batchId() {
  if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g,"");
  return "batch" + Date.now() + Math.floor(Math.random()*1e9).toString(36);
}
async function submitEvents(events, stableBatchId) {
  if (!Array.isArray(events) || !events.length || events.length > 100) {
    throw new Error("1~100개의 이벤트가 필요합니다");
  }
  const data = await call("/api/v1/requests/import", {
    method:"POST", body:JSON.stringify({batchId:stableBatchId, events})
  });
  await refreshRequests();
  await refreshEvents();
  const incomplete = data.requests.filter(r => r.status !== "SUCCEEDED");
  if (incomplete.length) throw new Error(incomplete.length + "건 반영 실패 또는 처리 중입니다. 같은 데이터로 재전송하세요. batchId: " + data.batchId);
  message(data.requests.length + "건을 Google Calendar에 등록했습니다.", "success");
}
el("singleForm").addEventListener("submit", async e => {
  e.preventDefault();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Seoul";
  const event = {
    subject:el("subject").value,
    startTime:{dateTime:el("start").value,timeZone:tz},
    endTime:{dateTime:el("end").value,timeZone:tz},
    chair:{displayName:el("chair").value},
    location:el("location").value
  };
  const fingerprint = JSON.stringify(event);
  if (!pendingSingle || pendingSingle.fingerprint !== fingerprint) {
    pendingSingle = {fingerprint, id:batchId()};
  }
  try { await submitEvents([event], pendingSingle.id); pendingSingle = null; e.target.reset(); }
  catch(err) { message(err.message, "error"); }
});
el("importText").addEventListener("input", () => { pendingBatchId=null; });
el("sendBatch").addEventListener("click", async () => {
  try {
    const input = JSON.parse(el("importText").value);
    const id = input && typeof input.batchId === "string"
      ? input.batchId : (pendingBatchId || (pendingBatchId = batchId()));
    if (input && Array.isArray(input.deletions)) {
      const data = await call("/api/v1/requests/delete-batch", {
        method:"POST", body:JSON.stringify({batchId:id, deletions:input.deletions})
      });
      await Promise.all([refreshRequests(), refreshEvents()]);
      const incomplete = data.requests.filter(r => r.status !== "SUCCEEDED");
      if (incomplete.length) throw new Error(incomplete.length + "건 삭제 실패 또는 처리 중입니다. 같은 데이터로 재전송하세요. batchId: " + id);
      message(data.requests.length + "건을 Google Calendar에서 삭제했습니다.", "success");
    } else {
      const events = Array.isArray(input) ? input : Array.isArray(input.events) ? input.events : [input];
      await submitEvents(events, id);
    }
    el("importText").value="";
    pendingBatchId=null;
  } catch(err) { message(err.message, "error"); }
});
el("exportReceipts").addEventListener("click", async () => {
  try {
    const data = await call("/api/v1/receipts");
    el("receiptText").value = JSON.stringify(data, null, 2);
    el("receiptText").focus();
    el("receiptText").select();
    message("처리 결과를 복사해 Sender에서 가져오세요.", "success");
  } catch(err) { message(err.message, "error"); }
});
el("refreshEvents").addEventListener("click", refreshEvents);
el("refreshRequests").addEventListener("click", refreshRequests);
el("refreshRejections").addEventListener("click", refreshRejections);
el("reconnectGoogle").addEventListener("click", async () => {
  if (!confirm("Google 동의 화면으로 이동합니다. 그룹 캘린더에 편집 권한이 있는 계정으로 진행하세요.")) return;
  try {
    const d = await call("/api/v1/admin/google/reconnect", {method: "POST"});
    location.href = d.url;
  } catch(e) { message(e.message, "error"); }
});
el("logout").addEventListener("click", async () => {
  try { await call("/api/v1/logout",{method:"POST"}); location.href="/login"; }
  catch(err){ message(err.message,"error"); }
});
(async () => {
  try {
    const user = await call("/api/v1/session");
    role = user.role; csrf = user.csrfToken;
    el("role").textContent = " · " + user.username + " (" +
      (role === "admin" ? "관리자" : role === "viewer" ? "조회자" : "제출자") + ")";
    if (role === "viewer") {
      el("singleForm").closest("section").hidden = true;
      el("importText").closest("section").hidden = true;
    }
    if (role === "admin") {
      el("adminSection").hidden = false;
      el("debugSection").hidden = false;
      refreshGoogleStatus();
      refreshRejections();
    }
    message("로그인 완료. 날짜/내용을 확인한 후 제출하세요.");
    await Promise.all([refreshEvents(),refreshRequests()]);
  } catch(e) { message(e.message,"error"); }
})();
