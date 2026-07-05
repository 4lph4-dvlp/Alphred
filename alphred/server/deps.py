"""게이트웨이 공유 컨텍스트 + 요청 헬퍼 (라우터 모듈 공용).

`create_app` 이 `GatewayDeps` 를 만들어 각 `routes_*.build_router(deps)` 에 주입한다.
핸들러가 클로저로 캡처하던 cfg/mgr/store/client/light_harness 를 명시적으로 전달한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import classifier
from ..config import Config
from ..db import Store
from ..hermes_client import HermesClient
from ..models import TaskKind, TaskSource, TaskState
from ..queue_manager import QueueManager
from ..safety import BlockedPayloadError, RestartGuard

logger = logging.getLogger("alphred.gateway")

# Alphred 작업 상태 → OpenAI runs 상태 매핑
STATE_TO_RUN = {
    TaskState.AWAITING_INPUT.value: "needs_input",   # §34.4 답변 대기(POST /queue/{id}/answers)
    TaskState.PENDING.value: "queued",
    TaskState.IN_PROGRESS.value: "running",
    TaskState.PAUSED.value: "paused",
    TaskState.COMPLETED.value: "completed",
    TaskState.NEEDS_REVIEW.value: "completed",   # run 은 끝남; 검증 플래그는 /queue 에 노출
    TaskState.DISCARDED.value: "cancelled",
}

_SOURCES = {s.value for s in TaskSource}


@dataclass
class GatewayDeps:
    """라우터 핸들러가 공유하는 런타임 의존성."""
    cfg: Config
    mgr: QueueManager
    store: Store
    client: HermesClient
    guard: RestartGuard | None
    light_harness: str


def make_auth(cfg: Config):
    """Bearer 인증 의존성(§35.1 스코프 인식).

    · 키 전무(레거시 단일 키도, 클라이언트 키도 없음) → 통과(개발 모드, 현행 유지)
    · 레거시 단일 키(API_SERVER_KEY/ALPHRED_API_KEY) 일치 → control(하위호환)
    · 클라이언트 키(clients.json) 일치 → 그 키의 스코프
    · read 스코프는 GET 만 허용 — 변경류(POST/DELETE)는 403(모니터링 전용 기기 키)
    """
    from .. import clientkeys

    def auth(request: Request,
             authorization: str | None = Header(default=None)) -> None:
        legacy = cfg.api_key
        has_clients = clientkeys.any_keys(cfg.alphred_home)
        if not legacy and not has_clients:
            return                                   # 개발 모드(키 미구성)
        presented = None
        if authorization and authorization.startswith("Bearer "):
            presented = authorization[7:]
        scope = None
        if legacy and presented == legacy:
            scope = "control"
        elif has_clients:
            scope = clientkeys.verify(cfg.alphred_home, presented)
        if scope is None:
            raise HTTPException(status_code=401, detail="invalid api key")
        if scope == "read" and request.method not in ("GET", "HEAD"):
            raise HTTPException(status_code=403,
                                detail="read-scope key: monitoring only (GET)")
    return auth


# ---- 요청 파싱 헬퍼(순수) ----
def overrides(request: Request):
    prio = request.headers.get("x-alphred-priority")
    kind = request.headers.get("x-alphred-kind")
    return (int(prio) if prio and prio.isdigit() else None,
            kind if kind in ("light", "heavy") else None)


def depth_ov(request: Request):
    d = (request.headers.get("x-alphred-depth") or "").lower()
    return d if d in ("low", "mid", "high") else None


def source(request: Request, default: str) -> str:
    # 하위 서비스(MCP 등) 트리거가 출처를 태깅할 수 있다(감사/우선순위).
    s = request.headers.get("x-alphred-source", "")
    return s if s in _SOURCES else default


def extract(body: dict) -> tuple[str, bool]:
    """요청에서 (텍스트, 오디오포함) 를 뽑는다(멀티모달 인식, 기획 3.2).

    OpenAI 멀티모달 content 파트(text/input_text, image_url/input_image,
    input_audio)를 처리한다. 텍스트는 분류·큐에 쓰이고, 오디오 존재는
    라우팅 힌트(텍스트 없는 음성 → Light)로 쓴다.
    """
    def from_content(content) -> tuple[str, bool]:
        if isinstance(content, str):
            return content, False
        txt, aud = [], False
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                t = part.get("type", "")
                if t in ("text", "input_text"):
                    txt.append(part.get("text", ""))
                elif t in ("input_audio", "audio"):
                    aud = True
        return " ".join(s for s in txt if s), aud

    raw = body.get("input")
    if isinstance(raw, str):
        return raw, False
    if isinstance(raw, list) and raw and isinstance(raw[-1], dict):
        return from_content(raw[-1].get("content", raw[-1]))
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        return from_content(msgs[-1].get("content", ""))
    return "", False


def extract_attachments(body: dict) -> list[dict] | None:
    """마지막 사용자 메시지의 이미지 파트를 Hermes 표준형으로 수집(§37 Heavy 멀티모달 보존).

    Heavy 큐 경로는 텍스트만 저장하므로 image_url/input_image 파트를
    {"type":"image_url","image_url":{"url",...}} 표준형으로 정규화해 별도 보존한다
    (Hermes /v1/runs 는 배열 input 을 정규화 없이 그대로 쓰므로 표준형이 필요 — 실측).
    오디오/파일 파트는 Hermes /v1/runs 미지원 → 제외(오디오는 Light 로 라우팅됨).
    """
    raw = body.get("input")
    content = None
    if isinstance(raw, list) and raw and isinstance(raw[-1], dict):
        content = raw[-1].get("content", raw[-1])
    else:
        msgs = body.get("messages")
        if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
            content = msgs[-1].get("content")
    if not isinstance(content, list):
        return None
    out: list[dict] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in ("image_url", "input_image"):
            continue
        # chat 형(image_url={"url",...}) 과 responses 형(image_url=str) 모두 수용
        ref = part.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else ref
        if not (isinstance(url, str) and url.strip()):
            continue
        img: dict = {"type": "image_url", "image_url": {"url": url.strip()}}
        detail = ref.get("detail") if isinstance(ref, dict) else part.get("detail")
        if isinstance(detail, str) and detail.strip():
            img["image_url"]["detail"] = detail.strip()
        out.append(img)
    return out or None


def route_kind_hint(text: str, has_audio: bool, kind: str | None) -> str | None:
    # 텍스트 없는 음성 명령은 실시간 상호작용 → Light 로 라우팅(전사는 Hermes 가 수행)
    if kind is None and has_audio and not text.strip():
        return TaskKind.LIGHT.value
    return kind


def submit(mgr: QueueManager, text, **kw):
    """mgr.submit 래퍼 — 라이프사이클 차단(#30719)은 403 으로 변환."""
    try:
        return mgr.submit(text, **kw)
    except BlockedPayloadError as e:
        raise HTTPException(status_code=403,
                            detail={"error": "blocked_payload", "reason": e.reason,
                                    "matched": e.matched})


def apply_light_harness(body: dict, endpoint: str, request: Request, light_harness: str) -> None:
    """§29.2 Light 요청에 Alphred 시스템 메시지를 주입(콜드스타트 해소).

    주입 안 함: 하네스 off / `X-Alphred-Harness: off` / 호출자가 이미 system 제공.
    chat → messages 앞에 system, responses → `instructions`(없을 때만).
    """
    if not light_harness:
        return
    if (request.headers.get("x-alphred-harness") or "").lower() == "off":
        return
    if endpoint == "chat":
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return
        if any(isinstance(m, dict) and m.get("role") == "system" for m in msgs):
            return  # 호출자 system 존중(verbatim 계약)
        body["messages"] = [{"role": "system", "content": light_harness}, *msgs]
    else:  # responses
        if body.get("instructions"):
            return
        body["instructions"] = light_harness


def context_of(body: dict, limit: int = 6, cut: int = 800) -> str | None:
    """§34.2 A2 — 요청 본문의 이전 대화(messages 마지막 제외)를 IntentCard 맥락 문자열로.

    "그거 마저 해줘" 류의 지시 대상 해석에 쓰인다. user/assistant 텍스트 턴만, 최근 limit 개.
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list) or len(msgs) < 2:
        return None
    lines = []
    for m in msgs[:-1]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):  # 멀티모달 파트 → 텍스트만
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
        if isinstance(content, str) and content.strip():
            lines.append(f"{m['role']}: {content.strip()[:200]}")
    ctx = "\n".join(lines[-limit:])
    return ctx[:cut] or None


def needs_input_response(task) -> JSONResponse:
    """§34.4 AwaitingInput 작업의 202 응답 — 질문(선택지+추천)을 동봉."""
    try:
        questions = json.loads(task.questions) if task.questions else []
    except Exception:
        questions = []
    return JSONResponse(status_code=202, content={
        "id": task.id, "status": "needs_input", "object": "alphred.task",
        "questions": questions, "input_deadline": task.input_deadline,
        "answer_endpoint": f"/queue/{task.id}/answers"})


def route_realtime(deps: GatewayDeps, body: dict, request: Request, src: str,
                   light_call, endpoint: str = "chat"):
    """실시간(chat/responses) 공통 라우팅.

    분류 결과가 Heavy 면 큐에 등록(202 — 인테이크 질문이 있으면 needs_input),
    Light 면 진행 중 Heavy 를 선점한 뒤 light_call(text) 로 Hermes 에 동기 위임한다.
    """
    prio, kind = overrides(request)
    text, has_audio = extract(body)
    kind = route_kind_hint(text, has_audio, kind)
    # §40 세션 식별 — body.session_id(비표준 확장) 또는 헤더. 없으면 원장/재작성 없이 종전 동작.
    sess = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    sess = (sess or request.headers.get("x-alphred-session") or "").strip() or None
    ctx = context_of(body)                      # §34.2 A2 — 이전 대화 맥락
    ctx = deps.mgr.session_context(sess, ctx)   # §40 + 세션 작업 원장(서버측 보완)
    k, p, reason, plan, intent = deps.mgr.classify_full(
        text, source=src, explicit_priority=prio, explicit_kind=kind, context=ctx)
    if k == TaskKind.HEAVY.value:
        task = submit(deps.mgr, text, source=src, priority=p, kind=k,
                      session_key=sess,
                      plan=plan, classify_reason=reason, depth=depth_ov(request),
                      intent=intent, context=ctx,
                      attachments=extract_attachments(body))  # §37 이미지 보존
        if task.state == TaskState.AWAITING_INPUT.value:
            return needs_input_response(task)   # §34.4 착수 전 질문
        return JSONResponse(status_code=202,
                            content={"id": task.id, "status": "queued", "object": "alphred.task"})
    with deps.mgr.light_scope():
        apply_light_harness(body, endpoint, request, deps.light_harness)
        return light_call(text)


# ---- SSE / 직렬화 헬퍼 ----
def sse_event(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def aiter(items):
    for it in items:
        yield it


def task_view(t) -> dict:
    plan = None
    if getattr(t, "plan", None):
        try:
            plan = json.loads(t.plan)
        except Exception:
            plan = None
    verify = None
    if getattr(t, "verify_report", None):
        try:
            verify = json.loads(t.verify_report)
        except Exception:
            verify = None
    intent = None
    if getattr(t, "intent", None):
        try:
            intent = json.loads(t.intent)
        except Exception:
            intent = None

    def _j(name):
        raw = getattr(t, name, None)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    return {
        "intent": intent,
        # §34.4 인테이크 — 질문(선택지+추천)/답변/채택 가정/답변 마감시각
        "questions": _j("questions"), "answers": _j("answers"),
        "assumptions": _j("assumptions"),
        "input_deadline": getattr(t, "input_deadline", None),
        "id": t.id, "state": t.state, "priority": t.priority, "kind": t.kind,
        "source": t.source, "prompt": t.prompt, "result": t.result,
        # §40 — 지시어 해소본(있으면 이것이 실행 기준)과 실존 산출물 경로
        "resolved_prompt": getattr(t, "resolved_prompt", None),
        "artifacts": _j("artifacts"),
        "session_key": getattr(t, "session_key", None),
        "classify_reason": t.classify_reason, "created_at": t.created_at,
        "depth": getattr(t, "depth", None), "verify_report": verify, "error": t.error,
        "verify_attempts": getattr(t, "verify_attempts", 0) or 0,
        "estimate": classifier.estimate_cost(plan, getattr(t, "depth", None) or "mid"),
        "plan": plan, "plan_progress": getattr(t, "plan_progress", 0) or 0,
        "plan_activity": getattr(t, "plan_activity", None),
    }
