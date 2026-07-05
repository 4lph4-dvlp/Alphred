"""§39: 카테고리 특화 모델 자동 라우팅 테스트."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from alphred.config import Config, build_model_routes
from alphred.classifier import classify, parse_intent, heuristic_category
from alphred.db import Store, new_id
from alphred.models import Task, TaskState, TaskKind
from alphred.queue_manager import QueueManager
from alphred.runtime import make_model_applier

class FakeClient:
    def __init__(self):
        self.started = []

    def start_run(self, prompt, **kwargs):
        self.started.append((prompt, kwargs))
        return f"fake_run_{new_id()}"


def test_heuristic_category():
    """사전필터 또는 IntentCard 실패 시 카테고리 휴리스틱 추정이 작동해야 한다."""
    assert heuristic_category("코드 작성해줘") == "coding"
    assert heuristic_category("1+1이 뭐야?") == "math"
    assert heuristic_category("이 문장 영어로 번역해줘") == "translation"
    assert heuristic_category("미국 주식 시장 요약 보고서 작성해줘") == "writing"
    assert heuristic_category("구글에서 오늘 날씨 찾아봐") == "research"
    assert heuristic_category("이 데이터 분석해서 차트 그려줘") == "analysis"
    assert heuristic_category("소설 하나 써줘") == "creative"
    assert heuristic_category("패키지 설치해라") == "agentic"
    assert heuristic_category("그냥 대화하자") == "general"


def test_parse_intent_with_category():
    """IntentCard 응답에 category가 포함되어 있을 때 올바르게 파싱되어야 한다."""
    llm_response = (
        '{"goal":"미국 주식 시장 요약 PDF 보고서 생성","domain":"document",'
        '"deliverable":{"type":"file","format":"pdf"},"kind":"heavy","priority":5,'
        '"depth":"high","category":"research","missing_info":[],"confidence":88}'
    )
    card = parse_intent(llm_response)
    assert card is not None
    assert card["category"] == "research"

    # 카테고리가 없거나 오염되었을 때 general 폴백
    llm_response_bad = (
        '{"goal":"미국 주식 시장 요약 PDF 보고서 생성","domain":"document",'
        '"deliverable":{"type":"file","format":"pdf"},"kind":"heavy","priority":5,'
        '"depth":"high","category":"invalid_cat","missing_info":[],"confidence":88}'
    )
    card_bad = parse_intent(llm_response_bad)
    assert card_bad is not None
    assert card_bad["category"] == "general"


def test_build_model_routes_includes_categories(tmp_path):
    """build_model_routes 가 카탈로그 내의 카테고리별 alias 를 올바르게 동동기화해야 한다."""
    alphred_home = tmp_path / "alphred"
    hermes_home = tmp_path / "hermes"
    alphred_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    config_yaml = hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: base-model\n", encoding="utf-8")

    routes = build_model_routes(alphred_home, hermes_home)
    # 기본 카탈로그(nvidia, openrouter 등)가 포함되어야 함
    assert "alphred-cat-coding" in routes
    assert routes["alphred-cat-coding"]["provider"] == "openrouter"
    assert "anthropic" in routes["alphred-cat-coding"]["model"]


def test_auto_model_routing_routes_mode(tmp_path):
    """models.json tier 가 "auto" 일 때, task.category 에 따라 올바른 category alias 가 사용되어야 한다."""
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

    # models.json 에서 high tier 를 auto 로 지정
    models_data = {
        "base": "base-model",
        "high": "auto"
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    # model_routes 빌드
    routes = build_model_routes(cfg.alphred_home, cfg.hermes_home)
    applier = make_model_applier(cfg, routes=routes)

    # coding 카테고리인 경우 -> alphred-cat-coding 반환
    model = applier("high", "coding")
    assert model == "alphred-cat-coding"

    # math 카테고리인 경우 -> alphred-cat-math 반환
    model2 = applier("high", "math")
    assert model2 == "alphred-cat-math"


def test_auto_model_routing_legacy_mode(tmp_path):
    """레거시 모드(routes=None)일 때 "auto" 센티널은 config.yaml을 카탈로그 모델로 직접 갱신해야 한다."""
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

    # models.json 에서 high tier 를 auto 로 지정
    models_data = {
        "base": "base-model",
        "high": "auto"
    }
    (cfg.alphred_home / "models.json").write_text(json.dumps(models_data), encoding="utf-8")

    applier = make_model_applier(cfg, routes=None)

    # coding 카테고리 적용 시 config.yaml 의 default model 이 nemotron coding 모델로 갱신되어야 함
    alias = applier("high", "coding")
    assert alias == "hermes-agent"

    content = config_yaml.read_text(encoding="utf-8")
    assert "anthropic/claude-3.5-sonnet" in content


def test_cli_scout_update(tmp_path, monkeypatch):
    """scout-update 서브커맨드가 올바르게 등록되고 동작해야 한다."""
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path / "alphred"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    (tmp_path / "alphred").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)

    config_yaml = tmp_path / "hermes" / "config.yaml"
    config_yaml.write_text("model:\n  default: base-model\n", encoding="utf-8")

    from alphred.cli import main
    from alphred import scout
    monkeypatch.setattr(scout, "run_scout_update", lambda *args, **kwargs: True)

    rc = main(["queue", "scout-update"])
    assert rc == 0

