"""자연어 기반 큐 관리 — 사용자의 자연어 요청을 큐 조작 액션으로 해석한다.

흐름: 사용자 요청 + 현재 큐 스냅샷 → LLM → {reply, actions[]} → 화이트리스트 액션 실행.
LLM 호출은 주입식 콜러블(`llm_call: str -> str`)이라 테스트 가능하다. 허용 액션은
status/reprioritize/discard/pause/resume 뿐이며, 그 외/미상 ID 는 무시한다.

CLI:     alphred queue ask "리포트 작업 우선순위 올려줘"
게이트웨이: POST /queue/ask {"q": "..."}
"""
from __future__ import annotations

from .jsonutil import parse_json_object
from .runtime import resolve_task_id

# 자연어로 조작 가능한 액션(화이트리스트). 그 외는 실행하지 않는다.
_ALLOWED = {"reprioritize", "discard", "pause", "resume", "status"}

INSTRUCTION = (
    "You manage a task queue. Given the current queue and the user's request "
    "(Korean or English), decide what to do. Allowed actions:\n"
    "  - reprioritize: change a task's priority (integer 1..10)\n"
    "  - discard: cancel/delete a task\n"
    "  - pause: pause a running task\n"
    "  - resume: allow a paused task to resume\n"
    "  - status: no change, just report\n"
    "Identify each task by the 8-char id shown in the list. Only act on tasks that appear "
    "in the list. If the user only asks about status, return an empty actions list and put "
    "the summary in 'reply'.\n"
    "Respond with ONLY a compact JSON object, no prose, no code fences:\n"
    '{"reply":"<short natural-language answer in the user\'s language>",'
    '"actions":[{"action":"reprioritize|discard|pause|resume","id":"<8-char id>",'
    '"priority":<int, only for reprioritize>}]}\n\n'
)


def snapshot(tasks) -> str:
    """LLM 에 넘길 큐 스냅샷(짧은 한 줄/작업)."""
    if not tasks:
        return "(empty)"
    lines = []
    for t in tasks:
        prompt = (t.prompt or "").replace("\n", " ")[:50]
        lines.append(f"- {t.id[:8]} prio={t.priority} state={t.state} "
                     f"kind={t.kind} :: {prompt}")
    return "\n".join(lines)


def build_prompt(request: str, tasks) -> str:
    # INSTRUCTION 에 리터럴 JSON 중괄호가 있어 str.format 대신 연결로 조립한다.
    return (INSTRUCTION + "Current queue:\n" + snapshot(tasks)
            + "\n\nUser request:\n" + (request or "")[:1000] + "\n")


def parse(text: str) -> dict | None:
    """LLM 응답에서 {reply, actions} 를 추출. 실패 시 None."""
    d = parse_json_object(text)
    if d is None:
        return None
    actions = d.get("actions")
    clean: list[dict] = []
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and str(a.get("action", "")).lower() in _ALLOWED:
                clean.append(a)
    return {"reply": str(d.get("reply", "")), "actions": clean}


def _resolve(store, prefix: str) -> str:
    """8자리 단축 ID 도 허용(cli._resolve_id 와 동일 규칙)."""
    return resolve_task_id(store, prefix)


def execute(mgr, store, actions) -> list[str]:
    """해석된 액션을 실행하고 사람이 읽을 결과 문자열 목록을 돌려준다."""
    results: list[str] = []
    for a in actions or []:
        act = str(a.get("action", "")).lower()
        if act in ("", "status"):
            continue
        rid = _resolve(store, str(a.get("id", "")))
        try:
            if act == "reprioritize":
                t = mgr.reprioritize(rid, int(a["priority"]))
                results.append(f"우선순위 변경: {t.id[:8]} → {t.priority}")
            elif act == "discard":
                t = mgr.discard(rid)
                results.append(f"폐기: {t.id[:8]} → {t.state}")
            elif act == "pause":
                t = mgr.pause(rid)
                results.append(f"일시중지: {t.id[:8]} → {t.state}")
            elif act == "resume":
                t = mgr.resume(rid)
                results.append(f"재개 허용: {t.id[:8]}")
        except (KeyError, ValueError) as e:
            results.append(f"실패({act} {str(a.get('id', ''))[:8]}): {e}")
    return results


def ask(mgr, store, request: str, llm_call) -> dict:
    """자연어 요청을 해석·실행. 반환: {reply, results, actions}."""
    tasks = mgr.list()
    text = ""
    try:
        text = llm_call(build_prompt(request, tasks)) or ""
    except Exception as e:  # LLM/네트워크 실패는 조용히 빈 응답으로
        return {"reply": f"(요청 해석 실패: {e})", "results": [], "actions": []}
    parsed = parse(text)
    if not parsed:
        return {"reply": text.strip() or "(응답을 해석하지 못했습니다)",
                "results": [], "actions": []}
    results = execute(mgr, store, parsed["actions"])
    return {"reply": parsed["reply"], "results": results, "actions": parsed["actions"]}


def make_hermes_llm(client, model: str = "hermes-agent"):
    """Hermes chat/completions 로 자연어 큐 요청을 처리하는 콜러블을 만든다."""
    def _call(prompt: str) -> str:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        resp = client.chat_completion(body)
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
    return _call
