
"use strict";
let csrf = "";
let role = "";
let pendingBatchId = null;

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
      if (role !== "viewer") button(item, "삭제 요청", async () => {
        if (!confirm("이 일정의 삭제 요청을 제출할까요? 실제 삭제는 관리자 승인 후 진행됩니다.")) return;
        await call("/api/v1/requests/delete", {
          method:"POST", body: JSON.stringify({eventId:e.id,expectedEtag:e.etag})
        });
        message("삭제 요청을 접수했습니다.", "success");
        await refreshRequests();
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
  message(data.requests.length + "건의 생성 요청을 접수했습니다. batchId: " + data.batchId, "success");
  await refreshRequests();
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
  try { await submitEvents([event], batchId()); e.target.reset(); }
  catch(err) { message(err.message, "error"); }
});
el("importText").addEventListener("input", () => { pendingBatchId=null; });
el("sendBatch").addEventListener("click", async () => {
  try {
    const input = JSON.parse(el("importText").value);
    const events = Array.isArray(input) ? input : Array.isArray(input.events) ? input.events : [input];
    if (!pendingBatchId) pendingBatchId = batchId();
    await submitEvents(events, pendingBatchId);
    el("importText").value="";
    pendingBatchId=null;
  } catch(err) { message(err.message, "error"); }
});
el("refreshEvents").addEventListener("click", refreshEvents);
el("refreshRequests").addEventListener("click", refreshRequests);
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
    message("로그인 완료. 날짜/내용을 확인한 후 제출하세요.");
    await Promise.all([refreshEvents(),refreshRequests()]);
  } catch(e) { message(e.message,"error"); }
})();
