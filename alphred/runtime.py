"""런타임 조립과 공통 큐 유틸리티."""
from __future__ import annotations

from .config import Config
from .db import Store
from .hermes_client import HermesClient
from .queue_manager import (
    QueueManager,
    make_hermes_classifier,
    make_hermes_judge,
    make_hermes_planner,
)


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
    client = HermesClient(cfg.api_base_url, cfg.api_key if api_key is None else api_key)
    llm = make_hermes_classifier(client) if cfg.llm_classify else None
    planner = make_hermes_planner(client) if cfg.planner else None
    judge = make_hermes_judge(client) if cfg.judge else None
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
    )
    return mgr, store, client
