"""§38 P2: Ensemble 멀티에이전트 병렬 처리, 프로바이더 cap, 세션 직렬화 테스트."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from alphred.config import Config
from alphred.db import Store, new_id
from alphred.models import Task, TaskState, TaskKind
from alphred.queue_manager import QueueManager
from alphred.budget import BUDGET_DEFAULTS
from alphred.cli import _collect_doctor

class FakeClient:
    def __init__(self):
        self.started = []

    def start_run(self, prompt, **kwargs):
        self.started.append((prompt, kwargs))
        return f"fake_run_{new_id()}"

    def stop_run(self, run_id):
        return {}


def test_session_serialization_gate(tmp_path):
    """같은 session_key를 가진 작업 2개는 병렬 실행되지 않고 직렬화되어야 한다."""
    store = Store(tmp_path / "t.db")
    fake = FakeClient()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD", max_slots=2)

    # 같은 session_key "sess-1"을 가진 두 Task 생성
    t1 = Task(id=new_id(), state=TaskState.PENDING.value, prompt="task1", session_key="sess-1", priority=9)
    t2 = Task(id=new_id(), state=TaskState.PENDING.value, prompt="task2", session_key="sess-1", priority=8)
    store.create(t1)
    store.create(t2)

    mgr.tick()
    # 첫 틱 실행 후, t1은 In-Progress, t2는 Pending 또는 Paused 여야 함 (세션 직렬화)
    assert len(store.in_progress()) == 1
    assert store.get(t1.id).state == TaskState.IN_PROGRESS.value
    assert store.get(t2.id).state == TaskState.PENDING.value


def test_provider_capacity_gate(tmp_path):
    """프로바이더 동시 run 수 예산 한도(rpm / est_run_rpm)를 넘지 않아야 한다."""
    cfg = Config(
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
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)
    (cfg.hermes_home / "config.yaml").write_text("model:\n  default: some-model\n", encoding="utf-8")

    # models.json 에 모든 tier 가 동일한 openrouter 모델을 사용하도록 설정
    # openrouter 의 기본 rpm 은 20.0, est_run_rpm 은 12.0 이므로 cap = 1 이다.
    models_data = {
        "base": "openai/gpt-4o-mini",
        "high": {"model": "openai/gpt-4o-mini", "provider": "openrouter"},
        "mid": {"model": "openai/gpt-4o-mini", "provider": "openrouter"},
        "low": {"model": "openai/gpt-4o-mini", "provider": "openrouter"},
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    store = Store(cfg.db_path)
    fake = FakeClient()
    mgr = QueueManager(store, fake, cfg.queue_md_path, cfg=cfg)

    # 서로 다른 session_key 를 가진 3개 Task 생성 (세션 직렬화는 통과하게 함)
    t1 = Task(id=new_id(), state=TaskState.PENDING.value, prompt="t1", session_key="s1", priority=9, depth="high")
    t2 = Task(id=new_id(), state=TaskState.PENDING.value, prompt="t2", session_key="s2", priority=8, depth="high")
    t3 = Task(id=new_id(), state=TaskState.PENDING.value, prompt="t3", session_key="s3", priority=7, depth="high")
    store.create(t1)
    store.create(t2)
    store.create(t3)

    mgr.tick()
    # 슬롯은 4개지만 openrouter cap 이 1 이므로 1개만 실행되어야 함
    assert len(store.in_progress()) == 1
    assert store.get(t1.id).state == TaskState.IN_PROGRESS.value
    assert store.get(t2.id).state == TaskState.PENDING.value


def test_max_slots_auto_calculation(tmp_path):
    """ALPHRED_SLOTS=auto 일 때 대기 Heavy 수, 프로바이더 cap 합, slots_max 중 최솟값으로 계산되는지 확인."""
    cfg = Config(
        hermes_home=tmp_path / "hermes",
        alphred_home=tmp_path / "alphred",
        db_path=tmp_path / "alphred" / "t.db",
        queue_md_path=tmp_path / "alphred" / "QUEUE.MD",
        hermes_bin=None,
        api_base_url="http://localhost:8642/v1",
        gateway_url="http://localhost:8643",
        api_key=None,
        slots="auto"
    )
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)
    (cfg.hermes_home / "config.yaml").write_text("model:\n  default: some-model\n", encoding="utf-8")

    # 1) 대기 작업이 0개일 때 -> slots = 1 (최소 max(1, ...))
    store = Store(cfg.db_path)
    fake = FakeClient()
    mgr = QueueManager(store, fake, cfg.queue_md_path, cfg=cfg)
    assert mgr.max_slots == 1

    # 2) 대기 작업이 5개, openrouter(cap=1) 단일 모델 사용 시 -> slots = 1 (프로바이더 cap에 제한됨)
    models_data = {
        "base": "openai/gpt-4o-mini",
        "high": {"model": "openai/gpt-4o-mini", "provider": "openrouter"},
        "mid": {"model": "openai/gpt-4o-mini", "provider": "openrouter"},
        "low": {"model": "openai/gpt-4o-mini", "provider": "openrouter"},
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    for i in range(5):
        store.create(Task(id=new_id(), state=TaskState.PENDING.value, prompt=f"t{i}", session_key=f"s{i}"))
    assert mgr.max_slots == 1

    # 3) 대기 작업이 5개, nvidia 모델들 사용 시 (nvidia cap = 3) -> slots = 3
    models_data = {
        "base": "nvidia/llama",
        "high": {"model": "nvidia/llama", "provider": "nvidia"},
        "mid": {"model": "nvidia/llama", "provider": "nvidia"},
        "low": {"model": "nvidia/llama", "provider": "nvidia"},
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")
    assert mgr.max_slots == 3


def test_doctor_concurrency_warning(tmp_path):
    """doctor 실행 시 max_concurrent_runs < slots + 2 이면 경고(ok=False)가 나와야 한다."""
    cfg = Config(
        hermes_home=tmp_path / "hermes",
        alphred_home=tmp_path / "alphred",
        db_path=tmp_path / "alphred" / "t.db",
        queue_md_path=tmp_path / "alphred" / "QUEUE.MD",
        hermes_bin=None,
        api_base_url="http://localhost:8642/v1",
        gateway_url="http://localhost:8643",
        api_key=None,
        slots="10"  # 슬롯이 10개인데 Hermes max_concurrent_runs 가 기본 10 이면 경고 대상 (10 < 10 + 2)
    )
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)
    
    # config.yaml 에 max_concurrent_runs 를 10으로 설정
    (cfg.hermes_home / "config.yaml").write_text(
        "model:\n  default: test-model\ngateway:\n  api_server:\n    max_concurrent_runs: 10\n",
        encoding="utf-8"
    )

    report = _collect_doctor(cfg)
    concurrency_check = next(c for c in report["checks"] if c["name"] == "Hermes 동시성 설정(F1)")
    assert concurrency_check["ok"] is False
    assert "vs slots_limit=10" in concurrency_check["detail"]

    # max_concurrent_runs 를 12 로 늘리면 통과해야 함 (12 >= 10 + 2)
    (cfg.hermes_home / "config.yaml").write_text(
        "model:\n  default: test-model\ngateway:\n  api_server:\n    max_concurrent_runs: 12\n",
        encoding="utf-8"
    )
    report2 = _collect_doctor(cfg)
    concurrency_check2 = next(c for c in report2["checks"] if c["name"] == "Hermes 동시성 설정(F1)")
    assert concurrency_check2["ok"] is True
