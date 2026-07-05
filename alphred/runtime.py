"""런타임 조립과 공통 큐 유틸리티."""
from __future__ import annotations

import logging
import threading

from .capabilities import CapabilityRegistry
from .config import (Config, set_model_fields, set_reasoning_effort,
                     sync_model_routes, write_hermes_model_routes, _tier_for_depth,
                     read_catalog_file, DEFAULT_CATALOG)
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
    make_hermes_rewrite,
)
from .prompt import load_harness
from .queue_manager import QueueManager

logger = logging.getLogger("alphred.runtime")


def _yaml_scalar(v: str | None) -> str | None:
    """set_model_fields 인자용 — None 은 미기록, ''(복원용 빈 값)은 YAML '' 로 기록."""
    if v is None:
        return None
    return v if v else "''"


def make_model_applier(cfg: Config, routes: dict[str, dict] | None = None):
    """depth → model alias 반환 + reasoning_effort 적용기(§29.1, §38 P1).

    §38 P1 model_routes 모드(routes 비어있지 않음):
      config.yaml 편집 없이 alias 이름("alphred-low" 등)을 반환.
      start_run body 의 model 필드로 Hermes 가 route 를 선택.
      → 동시 디스패치 레이스 소멸.
    레거시 모드(routes 없음, tier 미설정):
      기존처럼 config.yaml model.default 라인편집 후 "hermes-agent" 반환.
    reasoning_effort 는 model_routes 에 없으므로 두 모드 모두 전역 config 편집 유지(§38.2-C).
    반환: model alias str (start_run body 의 model 파라미터).
    """
    lock = threading.Lock()
    state = {"current": None, "reasoning": None}
    use_routes = bool(routes)  # 기동 시 결정, 런타임 중 변경 없음

    def apply(depth, category: str | None = None) -> str:
        with lock:
            # ---- 모델 선택 ----
            model_alias = "hermes-agent"
            if use_routes:
                tier = _tier_for_depth(depth)
                spec = cfg.model_for_depth(depth) or {}
                if spec.get("model") == "auto":
                    cat = category or "general"
                    alias = f"alphred-cat-{cat}"
                else:
                    alias = f"alphred-{tier}"
                if alias in routes:  # type: ignore[operator]
                    model_alias = alias
            elif cfg.has_model_tiers():
                spec = cfg.model_for_depth(depth) or {}
                base = cfg.model_base_spec() or {}
                target = spec.get("model") or base.get("model")
                provider = spec.get("provider") or base.get("provider")
                base_url = spec.get("base_url") or base.get("base_url")

                if target == "auto":
                    catalog = read_catalog_file(cfg.alphred_home) or DEFAULT_CATALOG
                    cat = category or "general"
                    cat_info = catalog.get("categories", {}).get(cat, {}).get("primary") or {}
                    target = cat_info.get("model")
                    provider = cat_info.get("provider")
                    base_url = cat_info.get("base_url")

                if target:
                    key = (target, provider, base_url)
                    if state["current"] != key:
                        set_model_fields(cfg.hermes_home, default=target,
                                         provider=_yaml_scalar(provider),
                                         base_url=_yaml_scalar(base_url))
                        state["current"] = key
            # ---- reasoning_effort(두 모드 공통 — 전역 config 편집) ----
            if cfg.has_reasoning_tiers():
                r = cfg.reasoning_for_depth(depth)
                target_r = r if r is not None else cfg.reasoning_base_default()
                if state["reasoning"] != target_r:
                    set_reasoning_effort(cfg.hermes_home, target_r)
                    state["reasoning"] = target_r
            return model_alias

    return apply


def restore_base_model(cfg: Config) -> None:
    """종료 시 Hermes config.yaml 을 base(모델·provider·추론)로 복원 + routes 정리(§29.1, §38 P1).

    model_routes 모드에서는 model.default 복원이 불필요(routes 가 base 를 건드리지 않음).
    단 config.yaml 에 남긴 model_routes 블록을 제거해 독립 Hermes 사용에 잔류하지 않게 한다.
    reasoning_effort 는 전역이므로 항상 base 복원. 실패는 로그만(종료 경로 — 크래시 금지).
    """
    try:
        # §38 P1: model_routes 블록 제거(독립 Hermes 잔류 방지)
        write_hermes_model_routes(cfg.hermes_home, {})
        if cfg.has_model_tiers():
            base = cfg.model_base_spec()
            if base and base.get("model"):
                set_model_fields(cfg.hermes_home, default=base["model"],
                                 provider=_yaml_scalar(base.get("provider")),
                                 base_url=_yaml_scalar(base.get("base_url")))
        if cfg.has_reasoning_tiers():
            set_reasoning_effort(cfg.hermes_home, cfg.reasoning_base_default())
    except Exception:
        logger.warning("종료 시 base 모델 복원 실패(무시)", exc_info=True)


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
    routes = sync_model_routes(cfg.alphred_home, cfg.hermes_home)
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
        apply_model=make_model_applier(cfg, routes=routes),  # §29.1+§38 P1 depth별 모델 라우팅
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
        rewrite=(make_hermes_rewrite(client) if cfg.rewrite else None),  # §40 지시어 해소
        ledger_enabled=cfg.ledger,             # §40 세션 작업 원장(무LLM)
        cfg=cfg,
    )
    return mgr, store, client
