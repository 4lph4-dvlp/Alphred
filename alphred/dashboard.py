"""Alphred 대시보드 — 의존성 없는 단일 페이지(HTML+vanilla JS).

게이트웨이의 /queue API 를 사용한다. 드래그&드롭으로 우선순위를 재배정하고
일시중지/재개/폐기/제출을 수행한다. 2초마다 자동 새로고침.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alphred Queue</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --line:#2a2f3a; --fg:#e6e9ef; --mut:#8b93a7; --acc:#ffbf00; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 system-ui,Segoe UI,sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex; gap:14px; align-items:center; }
  header h1 { font-size:16px; margin:0; color:var(--acc); }
  header .key { margin-left:auto; }
  input,select,button,textarea { background:var(--panel); color:var(--fg); border:1px solid var(--line);
    border-radius:6px; padding:6px 9px; font:inherit; }
  button { cursor:pointer; } button:hover { border-color:var(--acc); }
  main { padding:20px; max-width:1100px; margin:0 auto; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; margin-bottom:18px; }
  .card h2 { font-size:13px; margin:0 0 10px; color:var(--mut); text-transform:uppercase; letter-spacing:.05em; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:middle; }
  th { color:var(--mut); font-weight:600; font-size:12px; }
  tr.row { cursor:grab; } tr.row.drag { opacity:.4; }
  .pill { padding:2px 8px; border-radius:20px; font-size:12px; border:1px solid var(--line); }
  .Pending{color:#9fb4ff;} .In-Progress{color:#5be37a;} .Paused{color:var(--acc);}
  .Completed{color:#7fd1a8;} .Discarded{color:#ff7a7a;} .NeedsReview{color:#ffb454;}
  .prio { width:56px; }
  .muted { color:var(--mut); }
  .act button { padding:3px 8px; font-size:12px; margin-right:4px; }
  .submit { display:flex; gap:8px; flex-wrap:wrap; }
  .submit textarea { flex:1; min-width:280px; min-height:38px; }
  .hint { color:var(--mut); font-size:12px; margin-top:6px; }
</style>
</head>
<body>
<header>
  <h1>⚡ Alphred Queue</h1>
  <span class="muted" id="conn">연결 확인 중…</span>
  <span class="key"><input id="apikey" placeholder="API key (없으면 비워둠)" size="22"></span>
</header>
<main>
  <div class="card">
    <h2>새 작업 제출</h2>
    <div class="submit">
      <textarea id="np" placeholder="작업 프롬프트…"></textarea>
      <select id="nk"><option value="">자동분류</option><option value="light">light</option><option value="heavy">heavy</option></select>
      <input id="npr" class="prio" type="number" min="1" max="10" placeholder="prio">
      <button onclick="submitTask()">제출</button>
    </div>
    <div class="hint">우선순위를 비우면 분류기가 자동 판정. light=즉시(선점), heavy=큐 등록.</div>
  </div>

  <div class="card">
    <h2>진행 / 대기 (드래그로 우선순위 조정)</h2>
    <table><thead><tr><th>≡</th><th>우선</th><th>상태</th><th>kind</th><th>작업</th><th>재시도</th><th>액션</th></tr></thead>
    <tbody id="active"></tbody></table>
  </div>

  <div class="card">
    <h2>종료 (최근)</h2>
    <table><thead><tr><th>우선</th><th>상태</th><th>kind</th><th>작업</th><th>결과</th></tr></thead>
    <tbody id="history"></tbody></table>
  </div>
</main>
<script>
const ACTIVE = ["Pending","In-Progress","Paused"];
const $ = s => document.querySelector(s);
function key(){ return $("#apikey").value.trim(); }
function hdr(extra){ const h = extra||{}; const k=key(); if(k) h["Authorization"]="Bearer "+k; return h; }
async function api(path, opts){
  opts = opts||{}; opts.headers = hdr(opts.headers);
  const r = await fetch(path, opts);
  if(!r.ok) throw new Error(r.status+" "+(await r.text()));
  return r.status===204?null:r.json();
}
const esc = s => (s||"").replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const cut = (s,n)=>{ s=s||""; return s.length>n? s.slice(0,n)+"…":s; };

let dragId = null;
async function load(){
  let data;
  try { data = await api("/queue"); $("#conn").textContent="● 연결됨"; $("#conn").style.color="#5be37a"; }
  catch(e){ $("#conn").textContent="○ 연결 끊김: "+e.message; $("#conn").style.color="#ff7a7a"; return; }
  const tasks = data.tasks;
  const act = tasks.filter(t=>ACTIVE.includes(t.state));
  const his = tasks.filter(t=>!ACTIVE.includes(t.state)).slice(0,30);
  $("#active").innerHTML = act.map(rowActive).join("") || `<tr><td colspan=7 class=muted>(없음)</td></tr>`;
  $("#history").innerHTML = his.map(rowHist).join("") || `<tr><td colspan=5 class=muted>(없음)</td></tr>`;
  bindDrag();
}
function rowActive(t){
  const dis = t.state==="Discarded"||t.state==="Completed";
  const pauseBtn = t.state==="In-Progress" ? `<button onclick="act('${t.id}','pause')">⏸</button>` :
                   t.state==="Paused" ? `<button onclick="act('${t.id}','resume')">▶</button>` : "";
  return `<tr class="row" draggable="true" data-id="${t.id}">
    <td class="muted">≡</td>
    <td><input class="prio" type="number" min=1 max=10 value="${t.priority}"
        onchange="setPrio('${t.id}',this.value)"></td>
    <td><span class="pill ${t.state}">${t.state}</span></td>
    <td>${t.kind}</td>
    <td title="${esc(t.prompt)}">${esc(cut(t.prompt,60))}</td>
    <td class="muted">${t.retries?("↻"+t.retries):""}</td>
    <td class="act">${pauseBtn}<button onclick="discard('${t.id}')">🗑</button></td>
  </tr>`;
}
function rowHist(t){
  return `<tr><td>${t.priority}</td><td><span class="pill ${t.state}">${t.state}</span></td>
    <td>${t.kind}</td><td title="${esc(t.prompt)}">${esc(cut(t.prompt,50))}</td>
    <td class="muted" title="${esc(t.result)}">${esc(cut(t.result,40))}</td></tr>`;
}
function bindDrag(){
  document.querySelectorAll("tr.row").forEach(tr=>{
    tr.ondragstart = e=>{ dragId = tr.dataset.id; tr.classList.add("drag"); };
    tr.ondragend = e=> tr.classList.remove("drag");
    tr.ondragover = e=> e.preventDefault();
    tr.ondrop = async e=>{ e.preventDefault(); await reorder(dragId, tr.dataset.id); };
  });
}
async function reorder(srcId, dstId){
  if(!srcId||srcId===dstId) return;
  const rows = [...document.querySelectorAll("tr.row")].map(r=>r.dataset.id);
  rows.splice(rows.indexOf(srcId),1);
  rows.splice(rows.indexOf(dstId),0,srcId);
  // 위에서부터 높은 우선순위 부여(최대 10)
  const n = rows.length;
  for(let i=0;i<n;i++){
    const pr = Math.max(1, Math.min(10, n-i));
    await api(`/queue/${rows[i]}/prio`,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({priority:pr})});
  }
  load();
}
async function setPrio(id,v){ await api(`/queue/${id}/prio`,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:+v})}); load(); }
async function act(id,what){ await api(`/queue/${id}/${what}`,{method:"POST"}); load(); }
async function discard(id){ if(!confirm("폐기할까요?"))return; await api(`/queue/${id}`,{method:"DELETE"}); load(); }
async function submitTask(){
  const prompt=$("#np").value.trim(); if(!prompt)return;
  const body={input:prompt}; const h={"Content-Type":"application/json"};
  const k=$("#nk").value, pr=$("#npr").value;
  if(k) h["X-Alphred-Kind"]=k; if(pr) h["X-Alphred-Priority"]=pr;
  await api("/v1/runs",{method:"POST",headers:h,body:JSON.stringify(body)});
  $("#np").value=""; $("#npr").value=""; load();
}
$("#apikey").value = localStorage.getItem("alphred_key")||"";
$("#apikey").onchange = ()=> localStorage.setItem("alphred_key",$("#apikey").value.trim());
load(); setInterval(load, 2000);
</script>
</body>
</html>
"""
