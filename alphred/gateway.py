"""Alphred Gateway — OpenAI 호환 HTTP 표면 + 백그라운드 스케줄러.

진입점 매핑:
  POST /v1/chat/completions   → Light(즉시, 선점 동반) — Hermes 로 동기 프록시
  POST /v1/responses          → Light(즉시, 선점 동반)
  POST /v1/runs               → Heavy(비동기) — 큐 등록 후 task_id 반환
  GET  /v1/runs/{id}          → Alphred 작업 상태(run 형식으로 매핑)
  GET  /v1/models             → Hermes 프록시
  /queue/*                    → Alphred 큐 관리 API

라우트는 그룹별 APIRouter 모듈(`server/routes_*.py`)에 있고, 여기서는 조립(create_app)과
서버 부팅/업스트림(:8642) 생명주기만 담당한다.
헤더 오버라이드: X-Alphred-Priority(1..10), X-Alphred-Kind(light|heavy).
인증: ALPHRED_API_KEY 또는 API_SERVER_KEY 설정 시 Bearer 토큰 필수(QA-7.8).
동시성: 핸들러는 sync(def) → FastAPI 스레드풀에서 실행, 상태변경은 QueueManager 락으로 직렬화.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import time
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Config
from .hermes_client import HermesClient
from .prompt import load_light_harness
from .queue_manager import QueueManager
from .runtime import build_manager
from .safety import RestartGuard
from .server import routes_admin, routes_models, routes_openai, routes_queue
from .server.deps import GatewayDeps

logger = logging.getLogger("alphred.gateway")


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
        # §34.5 능력 스냅샷 워밍업 — 첫 디스패치가 프로브 비용을 물지 않도록 백그라운드 수집.
        caps = getattr(mgr, "capabilities", None)
        if caps is not None:
            threading.Thread(target=lambda: _warm_caps(caps),
                             daemon=True, name="alphred-caps-warmup").start()
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
    # §29.2 Light(즉답) 하네스 — 동기 응답 앞에 system 메시지로 1회 주입(콜드스타트 해소).
    light_harness = load_light_harness(cfg.alphred_home) if cfg.light_harness else ""
    deps = GatewayDeps(cfg=cfg, mgr=mgr, store=store, client=client,
                       guard=guard, light_harness=light_harness)
    # 라우터 등록 순서(admin→models→openai→queue): 경로 그룹이 겹치지 않아 순서 무관하나,
    # 대시보드(무인증)를 admin 라우터가 먼저 붙인다.
    for mod in (routes_admin, routes_models, routes_openai, routes_queue):
        app.include_router(mod.build_router(deps))
    return app


def _warm_caps(caps) -> None:
    try:
        caps.snapshot()
    except Exception:
        logger.debug("capabilities 워밍업 실패", exc_info=True)


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def check_bind_safety(cfg: Config, host: str) -> str | None:
    """§35.1 — 비루프백 바인딩인데 인증이 전무하면 기동 거부 사유를 반환(안전 기본값)."""
    if _loopback(host):
        return None
    from . import clientkeys
    if cfg.api_key or clientkeys.any_keys(cfg.alphred_home):
        return None
    return (f"외부 바인딩(--host {host})에는 접속 키가 필요합니다. "
            "`alphred keys issue <기기이름>` 으로 키를 발급하거나 "
            "ALPHRED_API_KEY 를 설정한 뒤 다시 실행하세요. "
            "(로컬 전용은 --host 127.0.0.1)")


def serve(host: str = "127.0.0.1", port: int = 8643, interval: float = 1.0,
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
    refuse = check_bind_safety(cfg, host)   # §35.1 외부 바인딩 + 무인증 = 기동 거부
    if refuse:
        raise SystemExit("alphred: " + refuse)
    # Alphred↔Hermes 업스트림 인증 키. 재시작/다중 데몬이 같은 :8642 에 일관되게 붙도록 보존.
    hermes_key = cfg.api_key or _upstream_key(cfg)
    # Hermes 자동 기동 폭주 차단용 가드(게이트웨이 자체 안전망과 분리된 별도 파일).
    upstream_guard = RestartGuard(cfg.alphred_home / "hermes_restarts.json",
                                  cfg.restart_window_seconds, cfg.restart_threshold)
    ensure_upstream = _make_upstream_ensurer(cfg, hermes_key, auto_hermes, guard=upstream_guard)
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


def _make_upstream_ensurer(cfg: Config, hermes_key: str, auto_hermes: bool,
                           guard: "RestartGuard | None" = None):
    """:8642 준비 게이트(D1). 스케줄러가 매 틱 호출 → health 캐시(3s) + 미가동 시 자동
    (재)기동(20s 백오프). True=가동(처리), False=미가동(이번 틱 보류). 단일 경로라 갇히지 않음.

    guard 가 주어지면 재기동 폭주를 차단한다: 임계 초과 시 더는 재기동하지 않고 1회 경고만 남겨
    "Hermes 가 계속 못 떠 무한히 창/프로세스가 부활"하는 클래스를 제거한다.
    """
    state = {"proc": None, "ok": False, "last_check": 0.0, "last_spawn": 0.0,
             "tripped_warned": False}

    def ensure() -> bool:
        now = time.monotonic()
        if state["ok"] and (now - state["last_check"] < 3.0):
            return True  # 최근 health 캐시(틱마다 네트워크 호출 방지)
        state["last_check"] = now
        state["ok"] = _hermes_up(cfg, hermes_key)
        if state["ok"]:
            state["tripped_warned"] = False  # 회복되면 경고 상태 리셋
            return True
        if auto_hermes:
            p = state["proc"]
            if (p is None or p.poll() is not None) and (now - state["last_spawn"] > 20.0):
                if guard is not None and guard.tripped():
                    if not state["tripped_warned"]:
                        logger.warning("Hermes API 자동 기동이 %.0fs 내 %d회 실패 → 재기동 중단. "
                                       "`hermes gateway run` 으로 수동 확인 필요.",
                                       cfg.restart_window_seconds, cfg.restart_threshold)
                        state["tripped_warned"] = True
                    return False
                logger.info("Hermes API(%s) 미가동 → 자동 (재)기동", cfg.api_base_url)
                state["proc"] = _spawn_hermes_gateway(cfg, hermes_key)
                state["last_spawn"] = now
                if guard is not None:
                    guard.record_restart()
        return False

    ensure._state = state  # 종료 시 자식 프로세스 정리용
    return ensure


def _spawn_hermes_gateway(cfg: Config, key: str):
    """`hermes gateway run` 을 API 서버 활성화 상태로 **창 없이** 자식 프로세스 기동.

    출력은 hermes.log 로 리다이렉트한다. serve 가 (TUI 의 kill-on-close Job 안에서) 띄우므로
    이 자식도 같은 Job 을 상속 → TUI 종료 시 함께 정리된다.
    """
    if not cfg.hermes_bin:
        return None
    env = dict(os.environ)
    env["API_SERVER_ENABLED"] = "true"
    env["API_SERVER_KEY"] = key
    env.setdefault("PYTHONUTF8", "1")  # cp949 인코딩 문제 회피(한국어 Windows)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # §32 느린 모델 타임아웃 완화: Hermes 의 LLM 스트림 읽기(토큰간) 타임아웃 기본이 120s 라,
    # 느린 free-tier 70B 등이 그 안에 다음 토큰을 못 내면 APITimeoutError("Request timed out.")로
    # run 이 실패한다. Alphred 백그라운드 run 전용 게이트웨이에 상향값을 주입해 완주율을 높인다.
    # (전체 요청 상한 HERMES_API_TIMEOUT=1800s 는 그대로라 무한 대기는 아님.)
    env.setdefault("HERMES_STREAM_READ_TIMEOUT", str(cfg.stream_read_timeout))
    # §28 자율 실행: 이 게이트웨이는 Alphred 백그라운드 run 전용이라 대화형 승인자가 없다.
    # YOLO 로 execute_code/위험명령 승인대기→타임아웃 차단을 해소한다. 단 Hermes 하드라인
    # (rm -rf / · 셧다운 등)은 YOLO 보다 먼저 무조건 차단되고, Alphred 도 submit 시
    # safety.scan_payload 로 라이프사이클 명령을 차단하므로 방어층은 유지된다.
    if cfg.autonomous_exec:
        env["HERMES_YOLO_MODE"] = "1"
    logger.info("Hermes API 게이트웨이 자동 기동: %s gateway run (autonomous_exec=%s)",
                cfg.hermes_bin, cfg.autonomous_exec)
    try:
        log = open(cfg.alphred_home / "hermes.log", "ab")
        kwargs = {"env": env, "stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        return subprocess.Popen([cfg.hermes_bin, "gateway", "run"], **kwargs)
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
