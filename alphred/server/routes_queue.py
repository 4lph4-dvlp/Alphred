"""Alphred 큐 관리 라우트 — 조회/우선순위/일시중지/재개/재시도/폐기/영구삭제/자연어/라이브 스트림."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..models import TaskState
from .deps import GatewayDeps, aiter, make_auth, sse_event, task_view


def build_router(deps: GatewayDeps) -> APIRouter:
    router = APIRouter(dependencies=[Depends(make_auth(deps.cfg))])
    mgr, store, client = deps.mgr, deps.store, deps.client

    @router.get("/queue")
    def queue_list():
        return {"tasks": [task_view(t) for t in mgr.list()]}

    @router.get("/queue/{task_id}")
    def queue_get(task_id: str):
        t = mgr.get(task_id)
        if not t:
            raise HTTPException(status_code=404, detail="not found")
        return {**task_view(t), "events": store.events(t.id)}

    @router.post("/queue/{task_id}/prio")
    def queue_prio(task_id: str, body: dict):
        try:
            return task_view(mgr.reprioritize(task_id, int(body["priority"])))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/queue/{task_id}/pause")
    def queue_pause(task_id: str):
        try:
            return task_view(mgr.pause(task_id))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/queue/{task_id}/resume")
    def queue_resume(task_id: str):
        try:
            return task_view(mgr.resume(task_id))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/queue/{task_id}/retry")
    def queue_retry(task_id: str):
        try:
            return task_view(mgr.requeue(task_id))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/queue/{task_id}/answers")
    def queue_answers(task_id: str, body: dict):
        """§34.4 인테이크 답변 제출 — body {"answers":[...]} → AwaitingInput→Pending 승격.

        answers = 문자열 리스트(질문 순서) 또는 [{"q","answer"}]. 답변은 실행 입력에 주입된다.
        """
        answers = body.get("answers")
        if not isinstance(answers, (list, dict)) or not answers:
            raise HTTPException(status_code=400, detail="missing 'answers'")
        try:
            return task_view(mgr.answer(task_id, answers))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.delete("/queue/{task_id}")
    def queue_discard(task_id: str):
        try:
            return task_view(mgr.discard(task_id))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.delete("/queue/{task_id}/purge")
    def queue_purge(task_id: str):
        """작업 영구 삭제(복구 불가) — discard 와 달리 DB 에서 완전히 제거."""
        try:
            ok = mgr.purge(task_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"purged": ok, "id": task_id}

    @router.post("/queue/clear")
    def queue_clear():
        """종료된 작업(완료/검토/폐기)을 영구 삭제하고 삭제 건수 반환."""
        return {"cleared": mgr.clear_history()}

    @router.delete("/queue/by-session/{session_key}")
    def queue_purge_session(session_key: str):
        """세션에 종속된 모든 작업을 영구 삭제(세션 삭제 시 연쇄)."""
        return {"purged": mgr.purge_session(session_key), "session": session_key}

    @router.post("/queue/ask")
    def queue_ask(body: dict):
        """자연어 큐 관리 — {"q": "리포트 작업 우선순위 올려줘"} → 해석·실행."""
        from .. import nlq
        q = (body.get("q") or body.get("request") or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="missing 'q'")
        return nlq.ask(mgr, store, q, nlq.make_hermes_llm(client))

    @router.get("/queue/{task_id}/stream")
    async def queue_stream(task_id: str):
        """§33 실행 중 작업의 라이브 이벤트 SSE(도구·중간 텍스트·완료) — TUI 라이브 뷰용.

        Hermes /events 를 단일소비하는 `_track_run` 이 event_bus 로 팬아웃한 이벤트를 구독해 전달.
        실행 중이 아니면 현재 상태만 한 번 주고 닫는다.
        """
        t = mgr.get(task_id)
        if not t:
            raise HTTPException(status_code=404, detail="not found")
        bus = getattr(mgr, "event_bus", None)
        if bus is None or t.state != TaskState.IN_PROGRESS.value:
            return StreamingResponse(
                aiter([sse_event("state", {"state": t.state, "result": t.result or ""}),
                       sse_event("done", {})]),
                media_type="text/event-stream")
        q = bus.subscribe(task_id)

        async def gen():
            yield sse_event("state", {"state": TaskState.IN_PROGRESS.value})
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        cur = mgr.get(task_id)          # 종료 센티널 놓쳤어도 상태로 마감
                        if not cur or cur.state != TaskState.IN_PROGRESS.value:
                            break
                        yield b": keepalive\n\n"
                        continue
                    if ev is None:                      # 종료 센티널(run 끝)
                        break
                    yield sse_event(ev.get("event") or "event", ev)
            finally:
                bus.unsubscribe(task_id, q)
            final = mgr.get(task_id)
            yield sse_event("done", {"state": final.state if final else None,
                                     "result": (final.result if final else None) or ""})

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router
