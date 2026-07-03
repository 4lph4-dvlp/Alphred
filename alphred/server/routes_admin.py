"""관리/대시보드 라우트 — 대시보드(무인증) + 안전망(#30719, 인증)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..dashboard import DASHBOARD_HTML
from ..webchat import CHAT_HTML
from .deps import GatewayDeps, make_auth


def build_router(deps: GatewayDeps) -> APIRouter:
    router = APIRouter()          # 전역 auth 미부착 — 대시보드는 무인증
    auth = make_auth(deps.cfg)
    mgr, guard = deps.mgr, deps.guard

    # 대시보드 (페이지 자체는 무인증, API 호출은 JS 가 키 포함)
    @router.get("/", response_class=HTMLResponse)
    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    # §35.9 모드 c — 웹 챗봇(대화용, 큐 운영과 관심사 분리). 페이지 무인증·API 는 JS 가 키 포함.
    @router.get("/chat", response_class=HTMLResponse)
    def webchat():
        return HTMLResponse(CHAT_HTML)

    @router.get("/safety", dependencies=[Depends(auth)])
    def safety_status():
        return {"halted": mgr.halted, "reason": mgr.halt_reason,
                "restart_count": guard.count() if guard else None,
                "threshold": guard.threshold if guard else None,
                "window_seconds": guard.window if guard else None}

    @router.post("/safety/reset", dependencies=[Depends(auth)])
    def safety_reset():
        if guard:
            guard.reset()
        mgr.set_halted(False, "operator reset via /safety/reset")
        return {"halted": False, "restart_count": guard.count() if guard else None}

    # §34.5 능력 레지스트리 — 실물 스킬/툴/MCP/CLI/라이브러리 스냅샷(무LLM).
    @router.get("/capabilities", dependencies=[Depends(auth)])
    def capabilities():
        caps = getattr(mgr, "capabilities", None)
        if caps is None:
            return {"enabled": False}
        return {"enabled": True, **caps.summary()}

    @router.post("/capabilities/refresh", dependencies=[Depends(auth)])
    def capabilities_refresh():
        caps = getattr(mgr, "capabilities", None)
        if caps is None:
            return {"enabled": False}
        caps.invalidate()
        return {"enabled": True, **caps.summary()}

    return router
