"""OpenAI 호환 라우트 — 모델/스킬 프록시, chat/responses(Light), runs(Heavy 큐), plan, run 상태, TUI SSE."""
from __future__ import annotations

import httpx
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import classifier
from ..models import TaskKind, TaskSource, TaskState
from .deps import (GatewayDeps, STATE_TO_RUN, aiter, context_of, depth_ov, extract,
                   make_auth, overrides, route_realtime, source, sse_event, submit)

logger = logging.getLogger("alphred.gateway")


def build_router(deps: GatewayDeps) -> APIRouter:
    router = APIRouter(dependencies=[Depends(make_auth(deps.cfg))])
    mgr, client, cfg = deps.mgr, deps.client, deps.cfg

    @router.get("/v1/models")
    def models():
        return client.models()

    @router.get("/v1/skills")
    def skills():
        """설치된 Hermes 스킬 목록 — :8642 /v1/skills 로 프록시(TUI `/skills` 가 사용)."""
        try:
            return client.skills()
        except Exception as e:
            logger.warning("스킬 목록 조회 실패(:8642 미가동?): %s", e)
            return {"object": "list", "data": [], "error": str(e)}

    @router.post("/v1/chat/completions")
    def chat_completions(body: dict, request: Request):
        # Light 면 멀티모달 페이로드 원본 그대로 프록시(text 인자는 무시)
        return route_realtime(deps, body, request, TaskSource.CHAT.value,
                              lambda text: client.chat_completion(body), endpoint="chat")

    @router.post("/v1/responses")
    def responses(body: dict, request: Request):
        # Light 면 원본 body 그대로 프록시(멀티모달 input·previous_response_id 보존)
        return route_realtime(deps, body, request, TaskSource.API.value,
                              lambda text: client.respond_passthrough(body), endpoint="responses")

    @router.post("/v1/runs")
    def runs(body: dict, request: Request):
        prio, kind = overrides(request)
        text, _aud = extract(body)
        src = source(request, TaskSource.API.value)  # 하위 서비스 출처 태깅(MCP 등)
        # 세션: 같은 session_id 로 보내면 후속 Heavy run 들이 같은 Hermes 세션(서버측 맥락)을
        # 이어쓴다. 미지정 시 run 별 독립 세션(= run_id). conversation_history 로 명시 핸드오프도 가능.
        session_id = body.get("session_id") or None
        ch = body.get("conversation_history")
        history = ch if isinstance(ch, list) else None
        # §34.2 A2 — 동봉된 conversation_history 를 의도 판정 맥락으로도 활용
        ctx = context_of({"messages": [*(history or []), {"role": "user", "content": text}]})
        # §35.2 — 종결 시 webhook 알림(임베디드/외부 서비스가 폴링 없이 결과 수신)
        delivery = body.get("delivery") if isinstance(body.get("delivery"), dict) else None
        # runs 는 비동기 의미 → 큐 등록. kind/prio 미지정 시 분류기 자동 판정.
        task = submit(mgr, text, source=src, priority=prio, kind=kind,
                      session_key=session_id, conversation_history=history,
                      depth=depth_ov(request), context=ctx, delivery=delivery)
        payload = {"run_id": task.id, "status": STATE_TO_RUN.get(task.state, "queued"),
                   "kind": task.kind, "priority": task.priority,
                   "depth": task.depth, "session_id": session_id or task.id}
        if task.state == TaskState.AWAITING_INPUT.value:   # §34.4 착수 전 질문
            try:
                payload["questions"] = json.loads(task.questions or "[]")
            except Exception:
                payload["questions"] = []
            payload["input_deadline"] = task.input_deadline
            payload["answer_endpoint"] = f"/queue/{task.id}/answers"
        return JSONResponse(status_code=202, content=payload)

    @router.post("/plan")
    def plan_preview(body: dict, request: Request):
        """드라이런(§21 V3/§34.3) — 분류·계획(v2)·심화도·비용 견적만 반환(실행/큐등록 없음).

        Heavy 로 판정되고 플래너가 켜져 있으면 **실제 디스패치와 동일한 Plan v2**(능력 접지
        +갭 수리 포함)를 미리 보여준다. LLM 콜이 들 수 있으나(제출과 동일) 실행은 없다.
        """
        text = body.get("message") or body.get("input") or extract(body)[0]
        src = source(request, TaskSource.API.value)
        kind, prio, reason, plan, intent = mgr.classify_full(text, source=src)
        if kind == TaskKind.HEAVY.value:                 # §34.3 디스패치와 동일 경로 미리보기
            p2 = mgr.preview_plan(text, intent=intent)
            if p2:
                plan = p2
        depth = ((intent or {}).get("depth")
                 if (intent or {}).get("depth") in ("low", "mid", "high")
                 else classifier.plan_to_depth(plan, kind))
        est = classifier.estimate_cost(plan, depth,
                                       judge_enabled=getattr(mgr, "judge", None) is not None)
        return {"kind": kind, "priority": prio, "depth": depth,
                "classify_reason": reason, "plan": plan, "estimate": est,
                "intent": intent}

    @router.get("/v1/runs/{task_id}")
    def run_status(task_id: str):
        t = mgr.get(task_id)
        if not t:
            raise HTTPException(status_code=404, detail="run not found")
        out = {"run_id": t.id, "status": STATE_TO_RUN.get(t.state, t.state),
               "state": t.state, "priority": t.priority, "kind": t.kind,
               "depth": getattr(t, "depth", None),
               "needs_review": t.state == TaskState.NEEDS_REVIEW.value,
               "verify_attempts": getattr(t, "verify_attempts", 0) or 0,
               "session_id": t.session_key or t.id}
        if t.result:
            out["output"] = t.result
        if t.error:
            out["error"] = t.error
        if getattr(t, "verify_report", None):   # §21 검증·수용 결과(Tier0/judge/제안)
            try:
                out["verify_report"] = json.loads(t.verify_report)
            except Exception:
                pass
        return out

    @router.post("/chat/stream")
    async def chat_stream(body: dict):
        """전용 TUI용 SSE 스트리밍(기획 §16, 작업 과정 표시).

        분류 → Heavy: `queued` 이벤트 한 개. Light: 진행 중 Heavy 선점 후 Hermes 세션
        chat/stream(:8642)의 SSE(tool.started/completed, assistant.delta/completed 등)를
        그대로 릴레이. TUI 는 :8643 한 곳만 보면 되고(단일 클라이언트), 무거운 라이브 툴
        실행은 백그라운드 큐가 처리하므로 전경 렌더 부담은 작다.
        """
        message = (body.get("message") or "").strip()
        session_id = str(body.get("session_id") or "alphred-tui")
        model = body.get("model") if isinstance(body.get("model"), str) else None
        prio = body.get("priority") if isinstance(body.get("priority"), int) else None
        kind_in = body.get("kind") if body.get("kind") in ("light", "heavy") else None
        depth_in = body.get("depth") if body.get("depth") in ("low", "mid", "high") else None
        # §34.2 A2 — TUI 가 동봉한 최근 대화 맥락(IntentCard/인테이크 질문 입력에 사용)
        ctx = body.get("context") if isinstance(body.get("context"), str) else None
        if not message:
            return StreamingResponse(aiter([sse_event("error", {"message": "empty message"})]),
                                     media_type="text/event-stream")
        k, p, reason, plan, intent = mgr.classify_full(message, source=TaskSource.TUI.value,
                                                       explicit_priority=prio,
                                                       explicit_kind=kind_in, context=ctx)
        # 업스트림(:8642) 베이스/인증 — 세션 API 는 /v1 밖이라 /v1 를 떼고 키를 재사용
        base = cfg.api_base_url[:-3] if cfg.api_base_url.endswith("/v1") else cfg.api_base_url
        up_headers = {}
        try:
            av = client._http.headers.get("authorization")
            if av:
                up_headers["Authorization"] = av
        except Exception:
            pass

        async def gen():
            if k == TaskKind.HEAVY.value:
                task = submit(mgr, message, source=TaskSource.TUI.value, priority=p, kind=k,
                              session_key=session_id, plan=plan, classify_reason=reason,
                              depth=depth_in, intent=intent, context=ctx)
                if task.state == TaskState.AWAITING_INPUT.value:  # §34.4 착수 전 질문
                    try:
                        qs = json.loads(task.questions or "[]")
                    except Exception:
                        qs = []
                    yield sse_event("needs_input", {"id": task.id, "questions": qs,
                                                    "input_deadline": task.input_deadline})
                    yield sse_event("done", {})
                    return
                yield sse_event("queued", {"id": task.id, "priority": task.priority})
                yield sse_event("done", {})
                return
            mgr.light_begin()
            try:
                async with httpx.AsyncClient(base_url=base, headers=up_headers, timeout=300.0) as uc:
                    try:
                        sbody = {"id": session_id}
                        if model:
                            sbody["model"] = model  # /model 전환 시 새 세션을 그 모델로 생성
                        # §29.2 대화 세션에도 Light 하네스 주입 — 직답·과잉거절/도구남용 억제.
                        # 세션 system_prompt 는 생성 시 1회 저장돼 이후 모든 턴에 적용된다.
                        if deps.light_harness:
                            sbody["system_prompt"] = deps.light_harness
                        await uc.post("/api/sessions", json=sbody)  # 409=이미 존재(무시)
                    except Exception:
                        pass
                    async with uc.stream("POST", f"/api/sessions/{session_id}/chat/stream",
                                         json={"message": message}) as resp:
                        if resp.status_code != 200:
                            txt = (await resp.aread()).decode("utf-8", "replace")
                            yield sse_event("error", {"message": f"upstream {resp.status_code}: {txt[:200]}"})
                            return
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                yield chunk  # Hermes SSE 프레이밍 그대로 전달
            except Exception as e:
                yield sse_event("error", {"message": str(e)})
            finally:
                mgr.light_end()

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router
