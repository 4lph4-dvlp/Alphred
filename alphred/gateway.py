"""Alphred Gateway — OpenAI 호환 HTTP 표면 + 백그라운드 스케줄러.

진입점 매핑:
  POST /v1/chat/completions   → Light(즉시, 선점 동반) — Hermes 로 동기 프록시
  POST /v1/responses          → Light(즉시, 선점 동반)
  POST /v1/runs               → Heavy(비동기) — 큐 등록 후 task_id 반환
  GET  /v1/runs/{id}          → Alphred 작업 상태(run 형식으로 매핑)
  GET  /v1/models             → Hermes 프록시
  /queue/*                    → Alphred 큐 관리 API

헤더 오버라이드: X-Alphred-Priority(1..10), X-Alphred-Kind(light|heavy).
인증: ALPHRED_API_KEY 또는 API_SERVER_KEY 설정 시 Bearer 토큰 필수(QA-7.8).
동시성: 핸들러는 sync(def) → FastAPI 스레드풀에서 실행, 상태변경은 QueueManager 락으로 직렬화.
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import classifier
from .config import Config
from .dashboard import DASHBOARD_HTML
from .db import Store
from .hermes_client import HermesClient
from .models import TaskKind, TaskSource, TaskState
from .queue_manager import QueueManager
from .runtime import build_manager
from .safety import BlockedPayloadError, RestartGuard

logger = logging.getLogger("alphred.gateway")

# Alphred 작업 상태 → OpenAI runs 상태 매핑
_STATE_TO_RUN = {
    TaskState.PENDING.value: "queued",
    TaskState.IN_PROGRESS.value: "running",
    TaskState.PAUSED.value: "paused",
    TaskState.COMPLETED.value: "completed",
    TaskState.NEEDS_REVIEW.value: "completed",   # run 은 끝남; 검증 플래그는 /queue 에 노출
    TaskState.DISCARDED.value: "cancelled",
}


class Scheduler(threading.Thread):
    def __init__(self, mgr: QueueManager, interval: float = 1.0, cron=None):
        super().__init__(daemon=True, name="alphred-scheduler")
        self.mgr = mgr
        self.interval = interval
        self.cron = cron
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("scheduler started (interval=%ss)", self.interval)
        while not self._stop_event.is_set():
            try:
                if self.cron is not None:
                    self.cron.tick()      # 만료된 cron 작업을 큐로 편입
                self.mgr.tick()
            except Exception:
                logger.exception("scheduler tick 오류")
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


def create_app(cfg: Config | None = None, *, mgr: QueueManager | None = None,
               guard: RestartGuard | None = None, cron=None,
               scheduler_interval: float = 1.0) -> FastAPI:
    cfg = cfg or Config.load()
    if mgr is None:
        mgr, store, client = build_manager(cfg)
    else:
        store, client = mgr.store, mgr.client
    scheduler = Scheduler(mgr, scheduler_interval, cron=cron)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if guard is not None:
            count = guard.record_restart()
            if guard.tripped():
                mgr.set_halted(True, f"재시작 폭주 감지: {guard.window:.0f}초 내 {count}회. "
                                     "자동 시작/재개 중지. POST /safety/reset 으로 해제.")
        try:
            mgr.recover()  # 크래시 복구(QA-7.7)
        except Exception:
            logger.exception("startup recover 실패")
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            client.close()
            store.close()

    app = FastAPI(title="Alphred Gateway", version="0.1.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.mgr = mgr

    # ---- 인증 ----
    def auth(authorization: str | None = Header(default=None)) -> None:
        key = cfg.api_key
        if not key:
            return  # 개발 모드: 키 미설정 시 통과
        if authorization != f"Bearer {key}":
            raise HTTPException(status_code=401, detail="invalid api key")

    # ---- 분류 헬퍼 ----
    _SOURCES = {s.value for s in TaskSource}

    def _overrides(request: Request):
        prio = request.headers.get("x-alphred-priority")
        kind = request.headers.get("x-alphred-kind")
        return (int(prio) if prio and prio.isdigit() else None,
                kind if kind in ("light", "heavy") else None)

    def _source(request: Request, default: str) -> str:
        # 하위 서비스(MCP 등) 트리거가 출처를 태깅할 수 있다(감사/우선순위).
        s = request.headers.get("x-alphred-source", "")
        return s if s in _SOURCES else default

    def _submit(text, **kw):
        try:
            return mgr.submit(text, **kw)
        except BlockedPayloadError as e:
            raise HTTPException(status_code=403,
                                detail={"error": "blocked_payload", "reason": e.reason,
                                        "matched": e.matched})

    def _extract(body: dict) -> tuple[str, bool]:
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

    def _route_kind_hint(text: str, has_audio: bool, kind: str | None) -> str | None:
        # 텍스트 없는 음성 명령은 실시간 상호작용 → Light 로 라우팅(전사는 Hermes 가 수행)
        if kind is None and has_audio and not text.strip():
            return TaskKind.LIGHT.value
        return kind

    def _route_realtime(body: dict, request: Request, source: str, light_call):
        """실시간(chat/responses) 공통 라우팅.

        분류 결과가 Heavy 면 큐에 등록(202), Light 면 진행 중 Heavy 를 선점한 뒤
        light_call(text) 로 Hermes 에 동기 위임한다.
        """
        prio, kind = _overrides(request)
        text, has_audio = _extract(body)
        kind = _route_kind_hint(text, has_audio, kind)
        k, p, reason, plan = mgr.classify_full(text, source=source,
                                               explicit_priority=prio, explicit_kind=kind)
        if k == TaskKind.HEAVY.value:
            task = _submit(text, source=source, priority=p, kind=k,
                           plan=plan, classify_reason=reason)
            return JSONResponse(status_code=202,
                                content={"id": task.id, "status": "queued", "object": "alphred.task"})
        with mgr.light_scope():
            return light_call(text)

    # ---- 대시보드 (페이지 자체는 무인증, API 호출은 JS 가 키 포함) ----
    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    # ---- OpenAI 호환 ----
    @app.get("/v1/models", dependencies=[Depends(auth)])
    def models():
        return client.models()

    @app.get("/models/available", dependencies=[Depends(auth)])
    def models_available():
        """선택 가능한 실제 모델 목록 — Hermes 의 curated 모델 레지스트리(provider별)를 조회.

        :8642 의 /v1/models 는 메타모델(hermes-agent)만 주므로, config 의 현재 모델에서
        provider 를 추론해 Hermes venv 의 hermes_cli.models 로 큐레이션 목록을 가져온다.
        """
        model_cfg = _read_model_cfg(cfg)
        cur = model_cfg.get("default")
        # 실제 provider 는 model.provider 가 우선(예: default=google/gemma-... 인데 provider=nvidia).
        provider = model_cfg.get("provider") or (
            cur.split("/")[0] if cur and "/" in cur else None)
        info = _curated_models(cfg, provider)
        return {"current": cur, "provider": info.get("label") or provider,
                "models": info.get("models", [])}

    @app.get("/v1/skills", dependencies=[Depends(auth)])
    def skills():
        """설치된 Hermes 스킬 목록 — :8642 /v1/skills 로 프록시(TUI `/skills` 가 사용)."""
        try:
            return client.skills()
        except Exception as e:
            logger.warning("스킬 목록 조회 실패(:8642 미가동?): %s", e)
            return {"object": "list", "data": [], "error": str(e)}

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)])
    def chat_completions(body: dict, request: Request):
        # Light 면 멀티모달 페이로드 원본 그대로 프록시(text 인자는 무시)
        return _route_realtime(body, request, TaskSource.CHAT.value,
                               lambda text: client.chat_completion(body))

    @app.post("/v1/responses", dependencies=[Depends(auth)])
    def responses(body: dict, request: Request):
        # Light 면 원본 body 그대로 프록시(멀티모달 input·previous_response_id 보존)
        return _route_realtime(body, request, TaskSource.API.value,
                               lambda text: client.respond_passthrough(body))

    @app.post("/v1/runs", dependencies=[Depends(auth)])
    def runs(body: dict, request: Request):
        prio, kind = _overrides(request)
        text, _aud = _extract(body)
        source = _source(request, TaskSource.API.value)  # 하위 서비스 출처 태깅(MCP 등)
        # 세션: 같은 session_id 로 보내면 후속 Heavy run 들이 같은 Hermes 세션(서버측 맥락)을
        # 이어쓴다. 미지정 시 run 별 독립 세션(= run_id). conversation_history 로 명시 핸드오프도 가능.
        session_id = body.get("session_id") or None
        ch = body.get("conversation_history")
        history = ch if isinstance(ch, list) else None
        # runs 는 비동기 의미 → 큐 등록. kind/prio 미지정 시 분류기 자동 판정.
        task = _submit(text, source=source, priority=prio, kind=kind,
                       session_key=session_id, conversation_history=history)
        return JSONResponse(status_code=202,
                            content={"run_id": task.id, "status": "queued",
                                     "kind": task.kind, "priority": task.priority,
                                     "depth": task.depth,
                                     "session_id": session_id or task.id})

    @app.post("/plan", dependencies=[Depends(auth)])
    def plan_preview(body: dict, request: Request):
        """드라이런(§21 V3) — 분류·계획·심화도·비용 견적만 반환(실행/큐등록 없음).

        모호 입력이면 플래너 1콜이 들 수 있으나(실제 제출과 동일), 실행은 하지 않는다.
        """
        text = body.get("message") or body.get("input") or _extract(body)[0]
        source = _source(request, TaskSource.API.value)
        kind, prio, reason, plan = mgr.classify_full(text, source=source)
        depth = classifier.plan_to_depth(plan, kind)
        est = classifier.estimate_cost(plan, depth,
                                       judge_enabled=getattr(mgr, "judge", None) is not None)
        return {"kind": kind, "priority": prio, "depth": depth,
                "classify_reason": reason, "plan": plan, "estimate": est}

    @app.get("/v1/runs/{task_id}", dependencies=[Depends(auth)])
    def run_status(task_id: str):
        import json
        t = mgr.get(task_id)
        if not t:
            raise HTTPException(status_code=404, detail="run not found")
        out = {"run_id": t.id, "status": _STATE_TO_RUN.get(t.state, t.state),
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

    # ---- Alphred 큐 관리 ----
    @app.get("/queue", dependencies=[Depends(auth)])
    def queue_list():
        return {"tasks": [_task_view(t) for t in mgr.list()]}

    @app.get("/queue/{task_id}", dependencies=[Depends(auth)])
    def queue_get(task_id: str):
        t = mgr.get(task_id)
        if not t:
            raise HTTPException(status_code=404, detail="not found")
        return {**_task_view(t), "events": store.events(t.id)}

    @app.post("/queue/{task_id}/prio", dependencies=[Depends(auth)])
    def queue_prio(task_id: str, body: dict):
        try:
            return _task_view(mgr.reprioritize(task_id, int(body["priority"])))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/queue/{task_id}/pause", dependencies=[Depends(auth)])
    def queue_pause(task_id: str):
        try:
            return _task_view(mgr.pause(task_id))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/queue/{task_id}/resume", dependencies=[Depends(auth)])
    def queue_resume(task_id: str):
        try:
            return _task_view(mgr.resume(task_id))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/queue/{task_id}/retry", dependencies=[Depends(auth)])
    def queue_retry(task_id: str):
        try:
            return _task_view(mgr.requeue(task_id))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/queue/{task_id}", dependencies=[Depends(auth)])
    def queue_discard(task_id: str):
        try:
            return _task_view(mgr.discard(task_id))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/queue/ask", dependencies=[Depends(auth)])
    def queue_ask(body: dict):
        """자연어 큐 관리 — {"q": "리포트 작업 우선순위 올려줘"} → 해석·실행."""
        from . import nlq
        q = (body.get("q") or body.get("request") or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="missing 'q'")
        return nlq.ask(mgr, store, q, nlq.make_hermes_llm(client))

    @app.post("/chat/stream", dependencies=[Depends(auth)])
    async def chat_stream(body: dict):
        """전용 TUI용 SSE 스트리밍(기획 §16, 작업 과정 표시).

        분류 → Heavy: `queued` 이벤트 한 개. Light: 진행 중 Heavy 선점 후 Hermes 세션
        chat/stream(:8642)의 SSE(tool.started/completed, assistant.delta/completed 등)를
        그대로 릴레이. TUI 는 :8643 한 곳만 보면 되고(단일 클라이언트), 무거운 라이브 툴
        실행은 백그라운드 큐가 처리하므로 전경 렌더 부담은 작다.
        """
        import httpx
        message = (body.get("message") or "").strip()
        session_id = str(body.get("session_id") or "alphred-tui")
        model = body.get("model") if isinstance(body.get("model"), str) else None
        prio = body.get("priority") if isinstance(body.get("priority"), int) else None
        kind_in = body.get("kind") if body.get("kind") in ("light", "heavy") else None
        if not message:
            return StreamingResponse(_aiter([_sse_event("error", {"message": "empty message"})]),
                                     media_type="text/event-stream")
        k, p, reason, plan = mgr.classify_full(message, source=TaskSource.TUI.value,
                                               explicit_priority=prio, explicit_kind=kind_in)
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
                task = _submit(message, source=TaskSource.TUI.value, priority=p, kind=k,
                               session_key=session_id, plan=plan, classify_reason=reason)
                yield _sse_event("queued", {"id": task.id, "priority": task.priority})
                yield _sse_event("done", {})
                return
            mgr.light_begin()
            try:
                async with httpx.AsyncClient(base_url=base, headers=up_headers, timeout=300.0) as uc:
                    try:
                        sbody = {"id": session_id}
                        if model:
                            sbody["model"] = model  # /model 전환 시 새 세션을 그 모델로 생성
                        await uc.post("/api/sessions", json=sbody)  # 409=이미 존재(무시)
                    except Exception:
                        pass
                    async with uc.stream("POST", f"/api/sessions/{session_id}/chat/stream",
                                         json={"message": message}) as resp:
                        if resp.status_code != 200:
                            txt = (await resp.aread()).decode("utf-8", "replace")
                            yield _sse_event("error", {"message": f"upstream {resp.status_code}: {txt[:200]}"})
                            return
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                yield chunk  # Hermes SSE 프레이밍 그대로 전달
            except Exception as e:
                yield _sse_event("error", {"message": str(e)})
            finally:
                mgr.light_end()

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- 안전망(#30719) ----
    @app.get("/safety", dependencies=[Depends(auth)])
    def safety_status():
        return {"halted": mgr.halted, "reason": mgr.halt_reason,
                "restart_count": guard.count() if guard else None,
                "threshold": guard.threshold if guard else None,
                "window_seconds": guard.window if guard else None}

    @app.post("/safety/reset", dependencies=[Depends(auth)])
    def safety_reset():
        if guard:
            guard.reset()
        mgr.set_halted(False, "operator reset via /safety/reset")
        return {"halted": False, "restart_count": guard.count() if guard else None}

    return app


def _read_model_cfg(cfg) -> dict:
    """config.yaml 의 model 블록에서 스칼라 키(default/provider/base_url)를 읽는다."""
    import re
    out: dict = {}
    p = cfg.hermes_home / "config.yaml"
    if not p.exists():
        return out
    in_model = False
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln[:1] not in (" ", "\t") and ":" in ln:
            in_model = ln.startswith("model:")
        if in_model:
            m = re.match(r"\s+(default|provider|base_url):\s*(\S+)", ln)
            if m:
                out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def _read_default_model(cfg) -> str | None:
    """config.yaml 의 model.default 값을 읽는다(현재 모델)."""
    return _read_model_cfg(cfg).get("default")


def _curated_models(cfg, provider: str | None) -> dict:
    """Hermes venv 의 hermes_cli.models 로 provider별 큐레이션 모델 목록을 조회(shell-out)."""
    import json
    import os
    import subprocess
    from pathlib import Path
    if not provider or not cfg.hermes_bin:
        return {}
    pyexe = Path(cfg.hermes_bin).with_name("python.exe")
    if not pyexe.exists():
        return {}
    code = (
        "import json;from hermes_cli.models import curated_models_for_provider,"
        "normalize_provider,provider_label;p=normalize_provider(%r);"
        "print(json.dumps({'label':provider_label(p),"
        "'models':[m for m,_ in curated_models_for_provider(p)]}))" % provider
    )
    try:
        env = {**os.environ, "PYTHONUTF8": "1"}
        out = subprocess.run([str(pyexe), "-c", code], cwd=str(cfg.hermes_home / "hermes-agent"),
                             capture_output=True, text=True, timeout=15, env=env)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip())
    except Exception:
        pass
    return {}


def _sse_event(event: str, payload: dict) -> bytes:
    import json
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _aiter(items):
    for it in items:
        yield it


def _task_view(t) -> dict:
    import json
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
    return {
        "id": t.id, "state": t.state, "priority": t.priority, "kind": t.kind,
        "source": t.source, "prompt": t.prompt, "result": t.result,
        "classify_reason": t.classify_reason, "created_at": t.created_at,
        "depth": getattr(t, "depth", None), "verify_report": verify, "error": t.error,
        "verify_attempts": getattr(t, "verify_attempts", 0) or 0,
        "estimate": classifier.estimate_cost(plan, getattr(t, "depth", None) or "mid"),
        "plan": plan, "plan_progress": getattr(t, "plan_progress", 0) or 0,
        "plan_activity": getattr(t, "plan_activity", None),
    }


def serve(host: str = "0.0.0.0", port: int = 8643, interval: float = 1.0,
          *, auto_hermes: bool = True) -> None:
    """Alphred 게이트웨이 기동.

    auto_hermes=True 면 Hermes API 게이트웨이(:8642)가 떠 있지 않을 때 자식 프로세스로
    자동 기동한다 → `alphred serve` 한 번으로 전체 스택이 뜬다.

    D1(런타임 단일화): :8642 의 health/기동을 **스케줄러가 매 틱 직접 평가**하는 단일 게이트
    (`ensure_upstream`)로 통합. 별도 watcher 스레드/`pause_scheduling` 플래그의 이중구조를
    없애 "플래그가 안 풀려 영원히 보류되는" 갇힘 상태 클래스를 제거한다.
    """
    import uvicorn
    from .cron_intercept import CronIntercept
    cfg = Config.load()
    # Alphred↔Hermes 업스트림 인증 키. 재시작/다중 데몬이 같은 :8642 에 일관되게 붙도록 보존.
    hermes_key = cfg.api_key or _upstream_key(cfg)
    ensure_upstream = _make_upstream_ensurer(cfg, hermes_key, auto_hermes)
    mgr, store, client = build_manager(cfg, api_key=hermes_key, ensure_upstream=ensure_upstream)
    if not auto_hermes and not _hermes_up(cfg, hermes_key):
        logger.warning("Hermes API(%s) 에 연결할 수 없습니다. 먼저 `hermes gateway run` 으로 "
                       "API 서버를 띄우세요.", cfg.api_base_url)
    cron = CronIntercept(mgr, cfg.cron_jobs_path, cfg.cron_state_path)
    guard = RestartGuard(cfg.guard_path, cfg.restart_window_seconds, cfg.restart_threshold)
    app = create_app(cfg, mgr=mgr, guard=guard, cron=cron, scheduler_interval=interval)
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        proc = ensure_upstream._state.get("proc")
        if proc is not None:
            _stop_proc(proc)


def _gen_key() -> str:
    import secrets
    return secrets.token_hex(16)


def _upstream_key(cfg: Config) -> str:
    """Alphred↔Hermes(:8642) 업스트림 키를 파일에 보존(재시작·다중 데몬 간 일관)."""
    p = cfg.alphred_home / "upstream.key"
    try:
        k = p.read_text(encoding="utf-8").strip()
        if k:
            return k
    except Exception:
        pass
    k = _gen_key()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(k, encoding="utf-8")
    except Exception:
        pass
    return k


def _hermes_up(cfg: Config, key: str | None) -> bool:
    c = HermesClient(cfg.api_base_url, key, timeout=5.0)
    try:
        return c.health()
    finally:
        c.close()


def _make_upstream_ensurer(cfg: Config, hermes_key: str, auto_hermes: bool):
    """:8642 준비 게이트(D1). 스케줄러가 매 틱 호출 → health 캐시(3s) + 미가동 시 자동
    (재)기동(20s 백오프). True=가동(처리), False=미가동(이번 틱 보류). 단일 경로라 갇히지 않음.
    """
    import time
    state = {"proc": None, "ok": False, "last_check": 0.0, "last_spawn": 0.0}

    def ensure() -> bool:
        now = time.monotonic()
        if state["ok"] and (now - state["last_check"] < 3.0):
            return True  # 최근 health 캐시(틱마다 네트워크 호출 방지)
        state["last_check"] = now
        state["ok"] = _hermes_up(cfg, hermes_key)
        if state["ok"]:
            return True
        if auto_hermes:
            p = state["proc"]
            if (p is None or p.poll() is not None) and (now - state["last_spawn"] > 20.0):
                logger.info("Hermes API(%s) 미가동 → 자동 (재)기동", cfg.api_base_url)
                state["proc"] = _spawn_hermes_gateway(cfg, hermes_key)
                state["last_spawn"] = now
        return False

    ensure._state = state  # 종료 시 자식 프로세스 정리용
    return ensure


def _spawn_hermes_gateway(cfg: Config, key: str):
    """`hermes gateway run` 을 API 서버 활성화 상태로 자식 프로세스 기동."""
    import os
    import subprocess
    if not cfg.hermes_bin:
        return None
    env = dict(os.environ)
    env["API_SERVER_ENABLED"] = "true"
    env["API_SERVER_KEY"] = key
    env.setdefault("PYTHONUTF8", "1")  # cp949 인코딩 문제 회피(한국어 Windows)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    logger.info("Hermes API 게이트웨이 자동 기동: %s gateway run", cfg.hermes_bin)
    try:
        return subprocess.Popen([cfg.hermes_bin, "gateway", "run"], env=env)
    except Exception:
        logger.exception("Hermes 게이트웨이 기동 실패")
        return None


def _stop_proc(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    except Exception:
        logger.exception("Hermes 프로세스 종료 실패")
