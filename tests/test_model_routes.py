"""§38 P1: model_routes 전환 단위/통합 테스트."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from alphred.config import (
    Config,
    build_model_routes,
    write_hermes_model_routes,
    read_hermes_model_routes,
    sync_model_routes,
)
from alphred.runtime import make_model_applier
from alphred.db import Store, new_id
from alphred.models import Task, TaskState, TaskKind
from alphred.queue_manager import QueueManager

class FakeClient:
    def __init__(self):
        self.started = []

    def start_run(self, prompt, **kwargs):
        self.started.append((prompt, kwargs))
        return "fake_run_id"

    def stop_run(self, run_id):
        return {}


def test_build_model_routes_empty(tmp_path):
    alphred_home = tmp_path / "alphred"
    hermes_home = tmp_path / "hermes"
    alphred_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    config_yaml = hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: original-model\n", encoding="utf-8")

    routes = build_model_routes(alphred_home, hermes_home)
    # models.json 이 없으면 base로 폴백하므로 다 alphred-{tier}가 base_spec이 됨
    assert routes["alphred-high"] == {"model": "original-model"}
    assert routes["alphred-mid"] == {"model": "original-model"}
    assert routes["alphred-low"] == {"model": "original-model"}


def test_build_model_routes_with_tiers(tmp_path):
    alphred_home = tmp_path / "alphred"
    hermes_home = tmp_path / "hermes"
    alphred_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    config_yaml = hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: original-model\n", encoding="utf-8")

    models_data = {
        "base": "original-model",
        "high": {"model": "nvidia/llama-Nemotron-70b"},
        "mid": "openai/gpt-4o-mini",
        # low 는 지정 안 함 -> base로 매핑되어야 함
    }
    models_json = alphred_home / "models.json"
    models_json.write_text(json.dumps(models_data), encoding="utf-8")

    routes = build_model_routes(alphred_home, hermes_home)
    assert routes["alphred-high"] == {"model": "nvidia/llama-Nemotron-70b"}
    assert routes["alphred-mid"] == {"model": "openai/gpt-4o-mini"}
    assert routes["alphred-low"] == {"model": "original-model"}


def test_write_and_read_model_routes(tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    config_yaml = hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: my-model\n", encoding="utf-8")

    routes = {
        "alphred-high": {"model": "high-model", "provider": "nim"},
        "alphred-low": {"model": "low-model"}
    }

    # 쓰기
    changed = write_hermes_model_routes(hermes_home, routes)
    assert changed is True

    # 읽기 검증
    read_routes = read_hermes_model_routes(hermes_home)
    assert read_routes["alphred-high"] == {"model": "high-model", "provider": "nim"}
    assert read_routes["alphred-low"] == {"model": "low-model"}

    # 동일 내용 다시 쓰기 -> 멱등성 검사 (False 반환해야 함)
    changed_again = write_hermes_model_routes(hermes_home, routes)
    assert changed_again is False

    # 제거하기
    changed_remove = write_hermes_model_routes(hermes_home, {})
    assert changed_remove is True
    assert read_hermes_model_routes(hermes_home) == {}


def test_make_model_applier_routes_mode(tmp_path):
    cfg = Config(
        hermes_home=tmp_path / "hermes",
        alphred_home=tmp_path / "alphred",
        db_path=tmp_path / "alphred" / "t.db",
        queue_md_path=tmp_path / "alphred" / "QUEUE.MD",
        hermes_bin=None,
        api_base_url="http://localhost:8642/v1",
        gateway_url="http://localhost:8643",
        api_key=None
    )
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)

    config_yaml = cfg.hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: base-model\n", encoding="utf-8")

    routes = {
        "alphred-high": {"model": "high-model"},
        "alphred-mid": {"model": "mid-model"},
        "alphred-low": {"model": "low-model"},
    }

    applier = make_model_applier(cfg, routes=routes)

    # routes 모드에서는 config.yaml의 model.default를 건드리지 않아야 함
    # depth가 high면 alphred-high alias 반환
    alias = applier("high")
    assert alias == "alphred-high"

    # config.yaml의 default model이 유지되는지 검증
    content = config_yaml.read_text(encoding="utf-8")
    assert "default: base-model" in content


def test_make_model_applier_legacy_mode(tmp_path):
    cfg = Config(
        hermes_home=tmp_path / "hermes",
        alphred_home=tmp_path / "alphred",
        db_path=tmp_path / "alphred" / "t.db",
        queue_md_path=tmp_path / "alphred" / "QUEUE.MD",
        hermes_bin=None,
        api_base_url="http://localhost:8642/v1",
        gateway_url="http://localhost:8643",
        api_key=None
    )
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)

    config_yaml = cfg.hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: base-model\n", encoding="utf-8")

    # models.json 에 tier 설정
    models_data = {
        "base": "base-model",
        "high": "high-model"
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    applier = make_model_applier(cfg, routes=None)  # routes=None -> 레거시 모드

    alias = applier("high")
    assert alias == "hermes-agent"  # 레거시는 hermes-agent 반환

    # config.yaml 의 default model 이 high-model 로 갱신되어 있어야 함
    content = config_yaml.read_text(encoding="utf-8")
    assert "default: high-model" in content


def test_dispatch_uses_model_alias_arg(tmp_path):
    store = Store(tmp_path / "t.db")
    fake = FakeClient()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD")

    # apply_model stub 이 alias 를 반환하도록 설정
    mgr.apply_model = lambda depth: f"alphred-{depth}"

    task = Task(id=new_id(), state=TaskState.PENDING.value, prompt="run me", depth="high")
    store.create(task)

    mgr._start(task)
    assert len(fake.started) == 1
    prompt, kwargs = fake.started[0]
    assert kwargs.get("model") == "alphred-high"
