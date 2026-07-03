"""런타임 조립과 공통 큐 유틸리티."""
from __future__ import annotations

import threading

from .capabilities import CapabilityRegistry
from .config import Config, set_model_fields
from .db import Store
from .eventbus import RunEventBus
from .hermes_client import HermesClient
from .llm_calls import (
    make_hermes_clarify,
    make_hermes_classifier,
    make_hermes_intent,
    make_hermes_judge,
    make_hermes_moa,
    make_hermes_planner,
    make_hermes_planner_v2,
    make_hermes_ranker,
)
from .prompt import load_harness
from .queue_manager import QueueManager


def make_model_applier(cfg: Config):
    """depth → Hermes config.default(+provider/base_url) 적용기(§29.1).

    Alphred 단일슬롯 디스패치/Light 시작 직전에 호출되어, 그 run·Light 가 읽을 config.default 를
    해당 depth 의 모델로 맞춘다(Hermes 가 매 agent 생성 시 config 재읽기 — 기획 §29.0.1 실측).
    사용자가 tier 를 하나도 설정하지 않았으면 완전 무동작(config.yaml 무손질 = 동작 무변화).
    멱등: 목표 모델이 이미 적용돼 있으면 파일을 쓰지 않는다(mtime 캐시 유지).
    """
    lock = threading.Lock()
    state = {"current": None}

    def apply(depth):
        if not cfg.has_model_tiers():
            return
        spec = cfg.model_for_depth(depth)
        target = (spec or {}).get("model") or cfg.model_base_default()
        if not target:
            return
        provider = (spec or {}).get("provider")
        base_url = (spec or {}).get("base_url")
        with lock:
            if state["current"] == target and not provider and not base_url:
                return
            set_model_fields(cfg.hermes_home, default=target,
                             provider=provider, base_url=base_url)
            state["current"] = target

    return apply


def resolve_task_id(store: Store, prefix: str) -> str:
    """전체 ID 또는 고유 prefix 를 전체 ID 로 해석한다."""
    prefix = (prefix or "").strip()
    if store.get(prefix):
        return prefix
    for cand in store.list():
        if cand.id.startswith(prefix):
            return cand.id
    return prefix


def resolve_task_id_from_tasks(tasks, prefix: str) -> str | None:
    """API 응답/Task 목록에서 prefix 와 일치하는 작업 ID 를 찾는다."""
    prefix = (prefix or "").strip()
    for task in tasks or []:
        tid = task.get("id") if isinstance(task, dict) else getattr(task, "id", None)
        if tid and tid.startswith(prefix):
            return tid
    return None


def build_manager(
    cfg: Config,
    *,
    api_key: str | None = None,
    ensure_upstream=None,
) -> tuple[QueueManager, Store, HermesClient]:
    """Config 에서 Store/HermesClient/QueueManager 를 일관되게 조립한다."""
    store = Store(cfg.db_path)
    client = HermesClient(cfg.api_base_url, cfg.api_key if api_key is None else api_key,
                          timeout=cfg.client_timeout)
    llm = make_hermes_classifier(client) if cfg.llm_classify else None
    planner = make_hermes_planner(client) if cfg.planner else None
    planner2 = make_hermes_planner_v2(client) if cfg.planner else None  # §34.3 디스패치 계획
    judge = make_hermes_judge(client) if cfg.judge else None
    ranker = make_hermes_ranker(client) if cfg.rank else None
    moa = make_hermes_moa(client) if cfg.moa else None     # §29.4 high 한정 멀티에이전트 정제
    intent = make_hermes_intent(client) if cfg.intent else None  # §34.2 IntentCard(opt-in)
    # §34.4 인테이크 질문 — IntentCard 의 critical 부족정보가 트리거라 intent 도 켜져야 동작.
    clarify = make_hermes_clarify(client) if (cfg.clarify and cfg.intent) else None
    # §34.5 능력 레지스트리 — 스킬/툴셋/MCP/CLI/라이브러리 실물 스냅샷(무LLM, TTL 캐시).
    caps = CapabilityRegistry(cfg, client) if cfg.caps else None
    system_prompt = load_harness(cfg.alphred_home)   # §26 사용자 편집본 우선, 없으면 기본 하네스
    mgr = QueueManager(
        store,
        client,
        cfg.queue_md_path,
        max_retries=cfg.max_retries,
        retry_base_seconds=cfg.retry_base_seconds,
        llm_classify=llm,
        ensure_upstream=ensure_upstream,
        planner=planner,
        verify=cfg.verify,
        judge=judge,
        judge_max_retries=cfg.judge_max_retries,
        ranker=ranker,
        system_prompt=system_prompt,
        apply_model=make_model_applier(cfg),   # §29.1 depth별 모델 라우팅
        moa=moa,
        event_bus=RunEventBus(),               # §33 Heavy run 라이브 스트림 팬아웃
        capabilities=caps,                     # §34.5 동적 하네스 + 설치 후 무효화
        intent=intent,                         # §34.2 LLM-first 의도 판정(기본 off)
        clarify=clarify,                       # §34.4 인테이크 질문+추천답변(기본 off)
        clarify_timeout=cfg.clarify_timeout,
        planner2=planner2,                     # §34.3 Plan v2(디스패치 직전, 능력 접지)
        orchestrate=cfg.orchestrate,           # §34.6 StepRunner(high 한정, 기본 off)
        task_budget=cfg.task_budget,
        step_retries=cfg.step_retries,
        watchdog=cfg.watchdog,                 # §34.6 E3 실행 중 감시(기본 off)
        stall_seconds=cfg.stall_seconds,
        tool_fail_limit=cfg.tool_fail_limit,
        prefs_path=cfg.preferences_path,       # §34.4 C3 선호 기억
    )
    return mgr, store, client
