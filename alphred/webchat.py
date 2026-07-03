"""웹 챗봇 UI (§35.9 모드 c) — 의존성 없는 단일 페이지(HTML+vanilla JS).

대시보드(큐 운영)와 분리된 대화용 페이지. 게이트웨이 `/chat/stream`(SSE)을 fetch 스트림으로
소비해 도구 과정(회색)과 최종 답변(흰색)을 표시하고, `needs_input` 은 추천답변이 강조된
질문 카드로 렌더해 `/queue/{id}/answers` 로 제출한다. 세션·키는 localStorage 보관.
"""

CHAT_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alphred Chat</title>
<style>
  :root { --bg:#140707; --panel:#1a0a0a; --line:#B22232; --acc:#E63946; --amber:#FF9F45;
          --fg:#f3e9e4; --dim:#a89890; --ok:#7BC96F; --info:#8FD3FF; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.55 system-ui,Segoe UI,sans-serif; background:var(--bg);
         color:var(--fg); height:100vh; display:flex; flex-direction:column; }
  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:12px;
           align-items:center; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0; color:var(--acc); }
  #qstrip { font-size:12px; color:var(--dim); }
  header .right { margin-left:auto; display:flex; gap:8px; }
  input,button,textarea { background:var(--panel); color:var(--fg);
    border:1px solid var(--line); border-radius:8px; padding:7px 10px; font:inherit; }
  button { cursor:pointer; } button:hover { border-color:var(--acc); }
  #log { flex:1; overflow-y:auto; padding:18px 16px; max-width:900px; width:100%;
         margin:0 auto; }
  .msg { margin:10px 0; white-space:pre-wrap; word-break:break-word; }
  .user { color:var(--info); } .user::before { content:"› "; font-weight:700; }
  .bot::before { content:"◆ Alphred\A"; font-weight:700; color:var(--acc);
                 white-space:pre; }
  .proc { color:var(--dim); font-size:13px; margin:2px 0 2px 12px; }
  .note { color:var(--amber); font-size:13px; }
  .err { color:#ff6b4a; }
  .qcard { border:1px solid var(--amber); border-radius:10px; padding:12px 14px;
           margin:10px 0; background:#20100a; }
  .qcard h3 { margin:0 0 8px; font-size:14px; color:var(--amber); }
  .opt { display:block; width:100%; text-align:left; margin:5px 0; }
  .opt.rec { border-color:var(--acc); }
  .opt.rec::after { content:"  ✦ 추천"; color:var(--acc); font-weight:700; }
  .qfree { display:flex; gap:6px; margin-top:8px; }
  .qfree input { flex:1; }
  footer { padding:10px 16px 14px; border-top:1px solid var(--line); }
  .send { display:flex; gap:8px; max-width:900px; margin:0 auto; }
  .send textarea { flex:1; min-height:44px; max-height:140px; resize:vertical; }
</style>
</head>
<body>
<header>
  <h1>◆ Alphred Chat</h1>
  <span id="qstrip">큐 —</span>
  <div class="right">
    <input id="apikey" placeholder="접속 키 (alphred keys issue)" size="24">
    <button onclick="newSession()">새 대화</button>
    <a href="/" style="align-self:center;color:var(--dim);font-size:13px">큐 대시보드 →</a>
  </div>
</header>
<main id="log"></main>
<footer><div class="send">
  <textarea id="inp" placeholder="메시지…  (Enter 전송 · Shift+Enter 줄바꿈)"></textarea>
  <button id="sendbtn" onclick="send()">전송</button>
</div></footer>
<script>
const $ = s => document.querySelector(s);
const log = $("#log");
let history = [];              // {role, text} — context 동봉용(최근 6턴)
let pending = null;            // needs_input 진행 상태 {taskId, questions, answers, idx}
let sid = localStorage.getItem("alphred_web_sid") ||
          ("web-" + Math.random().toString(16).slice(2, 10));
localStorage.setItem("alphred_web_sid", sid);
$("#apikey").value = localStorage.getItem("alphred_key") || "";
$("#apikey").onchange = () => localStorage.setItem("alphred_key", $("#apikey").value.trim());

function el(cls, text) {
  const d = document.createElement("div");
  d.className = cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  return d;
}
function hdr(extra) {
  const h = extra || {}; const k = $("#apikey").value.trim();
  if (k) h["Authorization"] = "Bearer " + k;
  return h;
}
function newSession() {
  sid = "web-" + Math.random().toString(16).slice(2, 10);
  localStorage.setItem("alphred_web_sid", sid);
  history = []; pending = null; log.innerHTML = "";
  el("note", "새 대화를 시작했습니다.");
}
function ctxOf() {   // §34.2 A2 — 최근 6턴(800자컷)
  return history.slice(-6).map(m => m.role + ": " + m.text.slice(0, 200))
                .join("\n").slice(0, 800) || null;
}

async function send() {
  const inp = $("#inp");
  const msg = inp.value.trim();
  if (!msg) return;
  inp.value = "";
  // 답변 모드면 입력을 질문 답으로 소비(§34.4)
  if (pending) { answerWith(msg); return; }
  el("msg user", msg);
  history.push({role: "user", text: msg});
  $("#sendbtn").disabled = true;
  let botEl = null, buf = "", procBuf = "";
  const flushProc = () => { if (procBuf.trim()) { el("proc", "┊ " + procBuf.trim().slice(0, 400)); procBuf = ""; } };
  try {
    const r = await fetch("/chat/stream", {method: "POST",
      headers: hdr({"Content-Type": "application/json"}),
      body: JSON.stringify({message: msg, session_id: sid, context: ctxOf()})});
    if (r.status === 401) { el("err", "인증 실패 — 우측 상단에 접속 키를 입력하세요."); return; }
    if (r.status === 403) { el("err", "이 키는 모니터링 전용(read)입니다 — control 키가 필요합니다."); return; }
    if (!r.ok || !r.body) { el("err", "오류 HTTP " + r.status); return; }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let raw = "";
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      raw += dec.decode(value, {stream: true});
      let i;
      while ((i = raw.indexOf("\n\n")) >= 0) {
        const block = raw.slice(0, i); raw = raw.slice(i + 2);
        let ev = null, data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!ev) continue;
        let d = {}; try { d = data ? JSON.parse(data) : {}; } catch (e) {}
        handleEvent(ev, d, {
          delta: t => { procBuf += t; },
          final: t => { flushProc();
            const text = (t || procBuf || buf).trim(); procBuf = "";
            if (text) { botEl = el("msg bot", text); history.push({role: "assistant", text}); } },
          tool: line => { flushProc(); el("proc", line); },
        });
      }
    }
    flushProc();
  } catch (e) { el("err", "연결 오류: " + e); }
  finally { $("#sendbtn").disabled = false; }
}

function handleEvent(ev, d, out) {
  if (ev === "queued") {
    el("note", "⏳ 무거운 작업으로 큐에 등록됨 (id=" + (d.id || "").slice(0, 8) +
               "). 완료는 아래 큐 표시/대시보드에서 확인.");
  } else if (ev === "needs_input") {
    renderQuestions(d);
  } else if (ev === "assistant.delta" || ev === "message.delta") {
    out.delta(d.delta || d.content || d.text || "");
  } else if (ev === "assistant.completed" || ev === "message.completed") {
    out.final(d.content || null);
  } else if (ev === "tool.started") {
    out.tool("┊ 🔧 " + (d.tool_name || d.tool || "tool") + "…");
  } else if (ev === "tool.completed") {
    out.tool("┊ ✓ " + (d.tool_name || d.tool || "tool"));
  } else if (ev === "tool.failed") {
    out.tool("┊ ✗ " + (d.tool_name || d.tool || "tool") + " 실패");
  } else if (ev === "error") {
    el("err", "오류: " + (d.message || ""));
  }
}

// ---- §34.4 질문 카드 — 추천답변 강조·클릭 선택·자유 입력 ----
function renderQuestions(d) {
  pending = {taskId: d.id, questions: d.questions || [], answers: [], idx: 0};
  el("note", "❓ 착수 전에 몇 가지만 확인할게요 (선택하거나 아래 입력창에 직접 입력 · " +
             "그대로 두면 추천값으로 자동 진행)");
  showQuestion();
}
function showQuestion() {
  const q = pending.questions[pending.idx];
  if (!q) return;
  const card = document.createElement("div");
  card.className = "qcard";
  const h = document.createElement("h3");
  h.textContent = (q.header ? "[" + q.header + "] " : "") + q.q +
                  "  (" + (pending.idx + 1) + "/" + pending.questions.length + ")";
  card.appendChild(h);
  (q.options || []).forEach(o => {
    const b = document.createElement("button");
    b.className = "opt" + (o.recommended ? " rec" : "");
    b.textContent = o.label;
    b.onclick = () => answerWith(o.label);
    card.appendChild(b);
  });
  const hint = document.createElement("div");
  hint.className = "proc";
  hint.textContent = "직접 답하려면 아래 입력창에 쓰고 전송";
  card.appendChild(hint);
  log.appendChild(card); log.scrollTop = log.scrollHeight;
}
async function answerWith(text) {
  const q = pending.questions[pending.idx];
  pending.answers.push({q: q.q, answer: text});
  el("proc", "→ " + text);
  pending.idx += 1;
  if (pending.idx < pending.questions.length) { showQuestion(); return; }
  const p = pending; pending = null;
  try {
    const r = await fetch("/queue/" + p.taskId + "/answers", {method: "POST",
      headers: hdr({"Content-Type": "application/json"}),
      body: JSON.stringify({answers: p.answers})});
    if (r.ok) el("note", "✓ 답변 반영 — 작업이 실행 대기열에 등록되었습니다.");
    else el("err", "답변 전송 실패 HTTP " + r.status +
                   " — 대기시간이 지나면 추천값으로 자동 진행됩니다.");
  } catch (e) { el("err", "답변 전송 오류: " + e); }
}

// ---- 미니 큐 스트립 + 완료 알림 ----
let lastStates = {};
async function pollQueue() {
  try {
    const r = await fetch("/queue", {headers: hdr()});
    if (!r.ok) { $("#qstrip").textContent = "큐 — (키 필요)"; return; }
    const tasks = (await r.json()).tasks || [];
    const n = s => tasks.filter(t => t.state === s).length;
    $("#qstrip").textContent = "큐 — 대기 " + (n("Pending") + n("AwaitingInput")) +
      " · 진행 " + n("In-Progress") + " · 완료 " + n("Completed") +
      (n("NeedsReview") ? " · 검토필요 " + n("NeedsReview") : "");
    for (const t of tasks) {
      const prev = lastStates[t.id];
      if (prev && prev !== t.state && ["Completed", "NeedsReview"].includes(t.state)) {
        el(t.state === "Completed" ? "note" : "err",
           (t.state === "Completed" ? "✓ 작업 완료: " : "⚠ 검토 필요: ") +
           (t.prompt || "").slice(0, 60) + " — " + (t.result || "").slice(0, 160));
      }
      lastStates[t.id] = t.state;
    }
  } catch (e) { /* 폴링 실패 무시 */ }
}
setInterval(pollQueue, 5000); pollQueue();

$("#inp").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
el("note", "Alphred 웹 챗 — 짧은 질문은 즉답, 무거운 작업은 자동으로 백그라운드 큐로 갑니다.");
</script>
</body>
</html>
"""
