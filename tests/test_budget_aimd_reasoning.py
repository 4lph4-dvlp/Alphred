"""§38 P3: AIMD, RPD 원장 및 reasoning 게이트 테스트."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
import pytest

from alphred.config import Config
from alphred.db import Store, new_id
from alphred.models import Task, TaskState, TaskKind
from alphred.queue_manager import QueueManager
from alphred.budget import (
    record_request, check_rpd_limit, get_current_capacity,
    decrease_capacity, increase_capacity, _get_ledger_path
)


class FakeClient:
    def __init__(self):
        self.started = []

    def start_run(self, prompt, **kwargs):
        self.started.append((prompt, kwargs))
        return f"fake_run_{new_id()}"

    def get_run(self, run_id):
        return {"status": "completed", "output": "success response"}


def _cfg(tmp_path) -> Config:
    return Config(
        hermes_home=tmp_path / "hermes",
        alphred_home=tmp_path / "alphred",
        db_path=tmp_path / "alphred" / "t.db",
        queue_md_path=tmp_path / "alphred" / "QUEUE.MD",
        hermes_bin=None,
        api_base_url="http://localhost:8642/v1",
        gateway_url="http://localhost:8643",
        api_key=None,
        slots="4"
    )


def test_rpd_ledger_and_limit(tmp_path):
    """RPD 요청 횟수가 누적되고 한도 도달 시 게이팅이 동작해야 한다."""
    alphred_home = tmp_path / "alphred"
    alphred_home.mkdir(parents=True, exist_ok=True)

    # 1) 초기 상태: 한도 미도달
    assert check_rpd_limit(alphred_home, "openrouter") is False

    # 2) 요청 50회 누적 (OpenRouter 기본 한도 50 RPD)
    for _ in range(50):
        record_request(alphred_home, "openrouter")

    # 3) 한도 도달 확인
    assert check_rpd_limit(alphred_home, "openrouter") is True

    # 4) 다른 프로바이더는 영향 없음
    assert check_rpd_limit(alphred_home, "nvidia") is False


def test_aimd_capacity_scaling(tmp_path):
    """429 에러 시 용량 감소(AIMD MD), 성공 시 용량 복구(AIMD AI)가 작동해야 한다."""
    alphred_home = tmp_path / "alphred"
    alphred_home.mkdir(parents=True, exist_ok=True)

    # OpenRouter 기본 RPM=20, est_run_rpm=12 -> 초기 cap=1
    # Nvidia 기본 RPM=40, est_run_rpm=12 -> 초기 cap=3
    max_cap = 3
    assert get_current_capacity(alphred_home, "nvidia") == max_cap

    # 1) decrease_capacity 호출 (AIMD MD) -> 3 // 2 = 1
    new_cap = decrease_capacity(alphred_home, "nvidia")
    assert new_cap == 1
    assert get_current_capacity(alphred_home, "nvidia") == 1

    # 2) increase_capacity 호출 (AIMD AI) -> 1 + 1 = 2
    new_cap = increase_capacity(alphred_home, "nvidia")
    assert new_cap == 2
    assert get_current_capacity(alphred_home, "nvidia") == 2

    # 3) 다시 increase_capacity 호출 -> 2 + 1 = 3 (max_cap 도달)
    new_cap = increase_capacity(alphred_home, "nvidia")
    assert new_cap == 3
    assert get_current_capacity(alphred_home, "nvidia") == 3

    # 4) 초과해서 증가하지 않음
    new_cap = increase_capacity(alphred_home, "nvidia")
    assert new_cap == 3


def test_scheduler_gates_rpd_limit(tmp_path):
    """RPD 한도 도달 시 스케줄러가 해당 프로바이더의 작업을 디스패치하지 않아야 한다."""
    cfg = _cfg(tmp_path)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)

    # openrouter 한도 도달 상태로 만듦
    for _ in range(50):
        record_request(cfg.alphred_home, "openrouter")

    # models.json 에서 high tier 를 openrouter 모델로 매핑
    models_data = {
        "base": "hermes-agent",
        "high": {
            "model": "meta-llama/llama-3.1-70b-instruct:free",
            "provider": "openrouter"
        }
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    store = Store(cfg.db_path)
    client = FakeClient()
    mgr = QueueManager(store, client, cfg.queue_md_path, max_slots=4, cfg=cfg)

    # 1) openrouter 대상 high tier 작업 제출
    t1 = mgr.submit("openrouter 작업", kind="heavy", depth="high")
    assert t1.state == TaskState.PENDING.value

    # 2) hermes 대상 low tier 작업 제출 (RPD 한도 없음)
    t2 = mgr.submit("hermes 작업", kind="heavy", depth="low")
    assert t2.state == TaskState.PENDING.value

    # 스케줄러 실행
    mgr.tick()

    # t1 (openrouter)은 RPD 한도에 막혀 Pending 유지
    # t2 (hermes)는 디스패치 완료
    assert store.get(t1.id).state == TaskState.PENDING.value
    assert store.get(t2.id).state == TaskState.IN_PROGRESS.value


def test_reasoning_effort_gate(tmp_path):
    """서로 다른 reasoning_effort를 가진 작업들은 동시에 실행되지 못하도록 게이팅되어야 한다."""
    cfg = _cfg(tmp_path)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)

    # reasoning_effort 설정
    models_data = {
        "base": "hermes-agent",
        "high": {
            "model": "hermes-agent",
            "reasoning": "high"
        },
        "mid": {
            "model": "hermes-agent",
            "reasoning": "low"
        }
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    store = Store(cfg.db_path)
    # mock client that blocks run from finishing immediately
    class BlockingClient:
        def start_run(self, prompt, **kwargs):
            return "blocking_run"
    
    mgr = QueueManager(store, BlockingClient(), cfg.queue_md_path, max_slots=4, cfg=cfg)

    # 1) high tier 작업 제출 (reasoning: high)
    t1 = mgr.submit("high 작업", kind="heavy", depth="high")
    # 2) mid tier 작업 제출 (reasoning: low)
    t2 = mgr.submit("mid 작업", kind="heavy", depth="mid")
    # 3) 또 다른 high tier 작업 제출 (reasoning: high)
    t3 = mgr.submit("high 작업 2", kind="heavy", depth="high")

    mgr.tick()

    # t1이 IN_PROGRESS로 가동 중
    assert store.get(t1.id).state == TaskState.IN_PROGRESS.value
    # t2 (reasoning: low)는 t1 (reasoning: high)와 충돌하므로 PENDING 유지 (게이팅됨)
    assert store.get(t2.id).state == TaskState.PENDING.value
    # t3 (reasoning: high)는 t1과 reasoning 요구치가 같으므로 IN_PROGRESS로 동시 실행됨
    assert store.get(t3.id).state == TaskState.IN_PROGRESS.value
