"""§29 결과물 품질 — depth별 모델 라우팅 · Light 하네스 · tune · MoA 테스트.

라이브 LLM/네트워크 불필요(fake + tmp config). QA-29.1~29.4 인수기준에 대응.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from alphred import classifier, tune
from alphred.config import (
    Config,
    read_config_scalar,
    read_default_model,
    set_config_scalar,
    set_model_fields,
)
from alphred.db import Store, new_id
from alphred.gateway import create_app
from alphred.models import TaskState
from alphred.prompt import load_light_harness
from alphred.queue_manager import QueueManager
from alphred.runtime import make_model_applier

_CFG_YAML = (
    "model:\n  default: nvidia/base-model\n  provider: nvidia\n"
    "  base_url: https://integrate.api.nvidia.com/v1\n"
    "compression:\n  enabled: true\n  threshold: 0.5\n  protect_first_n: 3\n"
    "  protect_last_n: 20\n"
    "tools:\n  tool_search:\n    enabled: auto\n    threshold_pct: 10\n"
    "web:\n  backend: brave-free\n  search_backend: ''\n"
    "auxiliary:\n  compression:\n    provider: auto\n    model: ''\n"
)


def _cfg(tmp_path, monkeypatch, **env) -> Config:
    hh = tmp_path / "hermes"
    hh.mkdir(parents=True, exist_ok=True)
    (hh / "config.yaml").write_text(_CFG_YAML, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path / "alphred"))
    for k in ("ALPHRED_MODEL_HIGH", "ALPHRED_MODEL_MID", "ALPHRED_MODEL_LOW"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Config.load()


# ───────────────────────── §29.1 depth별 모델 라우팅 ─────────────────────────

def test_set_model_fields_idempotent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert read_default_model(cfg.hermes_home) == "nvidia/base-model"
    assert set_model_fields(cfg.hermes_home, default="nvidia/base-model") is False  # 동일 → 미기록
    assert set_model_fields(cfg.hermes_home, default="nvidia/strong-70b") is True
    assert read_default_model(cfg.hermes_home) == "nvidia/strong-70b"
    # provider/base_url 도 한 번에
    assert set_model_fields(cfg.hermes_home, provider="google",
                            base_url="https://g/v1") is True
    assert read_config_scalar(cfg.hermes_home, ["model", "provider"]) == "google"


def test_model_for_depth_precedence(tmp_path, monkeypatch):
    # env 우선
    cfg = _cfg(tmp_path, monkeypatch, ALPHRED_MODEL_HIGH="nvidia/llama-70b")
    assert cfg.model_for_depth("high") == {"model": "nvidia/llama-70b"}
    assert cfg.model_for_depth("mid") is None
    assert cfg.has_model_tiers() is True
    # models.json tier (env 없을 때)
    cfg2 = _cfg(tmp_path, monkeypatch)
    assert cfg2.has_model_tiers() is False
    cfg2.set_tier_model("mid", "nvidia/mid-model")
    assert cfg2.model_for_depth("mid") == {"model": "nvidia/mid-model"}
    # low/미지정(동기 Light 종류) → low tier 로 매핑
    cfg2.set_tier_model("low", {"model": "nvidia/small", "provider": "nvidia"})
    assert cfg2.model_for_depth("low")["model"] == "nvidia/small"


def test_set_tier_snapshots_base_and_get_tiers(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.set_tier_model("high", "nvidia/strong")
    tiers = cfg.get_tiers()
    assert tiers["base"] == "nvidia/base-model"          # 첫 tier 설정 시 base 스냅샷
    assert tiers["high"]["model"] == "nvidia/strong"
    assert tiers["mid"] is None
    cfg.set_tier_model("high", None)                      # 해제
    assert cfg.get_tiers()["high"] is None


def test_set_default_model_persists_and_clears_tiers(tmp_path, monkeypatch):
    """/model <이름> — 영구 기본값(config.default+base) 저장 + 깊이별 tier 해제(라우팅 무override)."""
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.set_tier_model("high", "nvidia/strong")           # 티어가 있던 상태
    cfg.set_tier_model("low", "nvidia/cheap")
    assert cfg.has_model_tiers() is True
    cfg.set_default_model("meta/llama-3.3-70b-instruct")
    assert read_default_model(cfg.hermes_home) == "meta/llama-3.3-70b-instruct"  # config.yaml 유지
    tiers = cfg.get_tiers()
    assert tiers["base"] == "meta/llama-3.3-70b-instruct"
    assert tiers["high"] is None and tiers["mid"] is None and tiers["low"] is None
    assert cfg.has_model_tiers() is False                 # 라우팅이 config.default 를 안 덮어씀
    # 구 키(light) 잔재도 제거됨
    from alphred.config import read_models_file
    assert "light" not in read_models_file(cfg.alphred_home)


def test_reasoning_model_ids_provider_scoped(tmp_path):
    """§33 추론 배지: provider(예 nvidia) 엔트리로 한정 + 정확 id 매칭(리셀러 오염 무시)."""
    import json as _json
    from alphred.server.routes_models import _is_reasoning, _reasoning_model_ids
    cache = {
        "nvidia": {"models": {
            "google/gemma-4-31b-it": {"reasoning": True},
            "meta/llama-3.3-70b-instruct": {"reasoning": False},
            "deepseek-ai/deepseek-v4-pro": {"reasoning": True},
        }},
        "reseller-x": {"models": {                 # 오염원: 같은 모델을 reasoning=True 로 표기
            "meta/llama-3.3-70b-instruct": {"reasoning": True},
        }},
    }
    (tmp_path / "models_dev_cache.json").write_text(_json.dumps(cache), encoding="utf-8")
    rset = _reasoning_model_ids(tmp_path, "nvidia")
    assert "google/gemma-4-31b-it" in rset and "deepseek-ai/deepseek-v4-pro" in rset
    assert "meta/llama-3.3-70b-instruct" not in rset       # nvidia 스코프 → 리셀러 오염 무시
    assert _is_reasoning("google/gemma-4-31b-it", rset) is True
    assert _is_reasoning("meta/llama-3.3-70b-instruct", rset) is False
    assert _reasoning_model_ids(tmp_path, "bogus") == set()   # 없는 provider → graceful
    assert _reasoning_model_ids(tmp_path, None) == set()


def test_models_default_endpoint(tmp_path, monkeypatch):
    """POST /models/default → 영구 기본값 설정 + config.yaml 반영, known 플래그 반환."""
    cfg = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "g.db")
    mgr = QueueManager(store, _FakeUp(), tmp_path / "QUEUE.MD")
    with TestClient(create_app(cfg, mgr=mgr, scheduler_interval=3600)) as tc:
        r = tc.post("/models/default", json={"model": "meta/llama-3.3-70b-instruct"})
        assert r.status_code == 200
        body = r.json()
        assert body["default"] == "meta/llama-3.3-70b-instruct"
        assert body["known"] is True                      # 큐레이션 조회 불가(테스트) → 검증 생략=True
        assert tc.post("/models/default", json={"model": ""}).status_code == 400
    assert read_default_model(cfg.hermes_home) == "meta/llama-3.3-70b-instruct"


def test_model_applier_writes_per_depth_and_noop_without_tiers(tmp_path, monkeypatch):
    # tier 미설정 → config.yaml 절대 안 건드림(완전 무변화)
    cfg = _cfg(tmp_path, monkeypatch)
    apply = make_model_applier(cfg)
    apply("high")
    assert read_default_model(cfg.hermes_home) == "nvidia/base-model"
    # high tier 설정 → high 작업은 그 모델로, 미설정 mid 는 base 로 복원
    cfg.set_tier_model("high", "nvidia/strong-70b")
    apply = make_model_applier(cfg)
    apply("high")
    assert read_default_model(cfg.hermes_home) == "nvidia/strong-70b"
    apply("mid")
    assert read_default_model(cfg.hermes_home) == "nvidia/base-model"


class _FakeRun:
    def __init__(self):
        self.started = []
        self._status = {}

    def start_run(self, prompt, **kw):
        rid = "run_" + new_id()[:8]
        self.started.append((rid, prompt, kw))
        self._status[rid] = {"status": "completed", "output": f"DONE:{prompt[:8]}"}
        return rid

    def get_run(self, run_id):
        return self._status[run_id]

    def stop_run(self, run_id):
        self._status[run_id] = {"status": "cancelled"}
        return {"status": "stopping"}

    def chat_completion(self, body):
        return {"choices": [{"message": {"content": "x"}}]}

    def close(self):
        pass


def test_start_applies_model_for_depth(tmp_path):
    calls = []
    store = Store(tmp_path / "q.db")
    mgr = QueueManager(store, _FakeRun(), tmp_path / "QUEUE.MD",
                       apply_model=lambda depth: calls.append(depth))
    mgr.submit("전체 코드베이스 리팩터링 작업", kind="heavy", priority=5, depth="high")
    mgr.tick()
    assert calls and calls[0] == "high"


# ───────────────────────── §29.2 Light 하네스 ─────────────────────────

def test_light_harness_loads_and_strips_comment():
    txt = load_light_harness(None)
    assert "<!--" not in txt and "Alphred" in txt and len(txt) > 40


class _FakeUp:
    def __init__(self):
        self.last_chat = None
        self.last_resp = None

    def start_run(self, prompt, **kw):
        rid = "run_" + new_id()[:8]
        return rid

    def get_run(self, run_id):
        return {"status": "running"}

    def stop_run(self, run_id):
        return {"status": "stopping"}

    def chat_completion(self, body):
        self.last_chat = body
        return {"choices": [{"message": {"content": "ANS"}}]}

    def respond_passthrough(self, body):
        self.last_resp = body
        return {"output_text": "ANS"}

    def models(self):
        return {"object": "list", "data": [{"id": "hermes-agent"}]}

    def close(self):
        pass


@pytest.fixture
def harness_client(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, ALPHRED_LIGHT_HARNESS="1")
    store = Store(tmp_path / "g.db")
    fake = _FakeUp()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD")
    app = create_app(cfg, mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        yield tc, fake


def test_light_harness_injected_on_chat(harness_client):
    tc, fake = harness_client
    r = tc.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    msgs = fake.last_chat["messages"]
    assert msgs[0]["role"] == "system" and "Alphred" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hello"}


def test_light_harness_not_injected_when_caller_has_system(harness_client):
    tc, fake = harness_client
    tc.post("/v1/chat/completions", json={"messages": [
        {"role": "system", "content": "CALLER"}, {"role": "user", "content": "hello"}]})
    msgs = fake.last_chat["messages"]
    assert len(msgs) == 2 and msgs[0]["content"] == "CALLER"


def test_light_harness_opt_out_header(harness_client):
    tc, fake = harness_client
    tc.post("/v1/chat/completions", headers={"X-Alphred-Harness": "off"},
            json={"messages": [{"role": "user", "content": "hello"}]})
    assert fake.last_chat["messages"][0]["role"] == "user"


def test_light_harness_injected_into_chat_session(tmp_path, monkeypatch):
    """§29.2 갭 수정: TUI 대화(/chat/stream) 세션 생성 시 system_prompt 로 Light 하네스 주입."""
    import alphred.server.routes_openai as ro
    captured = {}

    class _FakeStream:
        status_code = 500

        async def aread(self):
            return b"down"

        async def aiter_bytes(self):
            return
            yield b""            # (도달 안 함) async generator 로 만들기 위한 형태

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeAC:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, path, json=None, **k):
            captured["path"] = path
            captured["body"] = json

            class _R:
                status_code = 201
            return _R()

        def stream(self, method, path, **k):
            return _FakeStream()

    monkeypatch.setattr(ro.httpx, "AsyncClient", _FakeAC)
    cfg = _cfg(tmp_path, monkeypatch, ALPHRED_LIGHT_HARNESS="1")
    store = Store(tmp_path / "g.db")
    mgr = QueueManager(store, _FakeUp(), tmp_path / "QUEUE.MD")
    with TestClient(create_app(cfg, mgr=mgr, scheduler_interval=3600)) as tc:
        tc.post("/chat/stream", json={"message": "안녕?", "session_id": "s1"})
    assert captured.get("path") == "/api/sessions"
    assert "Alphred" in captured["body"]["system_prompt"]   # 하네스가 세션에 주입됨


def test_light_harness_disabled_by_config(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, ALPHRED_LIGHT_HARNESS="0")
    store = Store(tmp_path / "g.db")
    fake = _FakeUp()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD")
    with TestClient(create_app(cfg, mgr=mgr, scheduler_interval=3600)) as tc:
        tc.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
    assert fake.last_chat["messages"][0]["role"] == "user"


# ───────────────────────── §29.3 alphred tune ─────────────────────────

def test_config_scalar_roundtrip_nested(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert read_config_scalar(cfg.hermes_home, ["tools", "tool_search", "threshold_pct"]) == "10"
    assert set_config_scalar(cfg.hermes_home, ["tools", "tool_search", "threshold_pct"], "25") is True
    assert read_config_scalar(cfg.hermes_home, ["tools", "tool_search", "threshold_pct"]) == "25"
    # 멱등
    assert set_config_scalar(cfg.hermes_home, ["tools", "tool_search", "threshold_pct"], "25") is False
    # 다른 블록은 안 건드림
    assert read_config_scalar(cfg.hermes_home, ["compression", "threshold"]) == "0.5"


def test_tune_audit_apply_revert(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    rep = tune.audit(cfg)
    by = {r["id"]: r for r in rep["rows"]}
    assert by["compress_first"]["action"] is True      # 3 != 6
    assert by["websearch"]["advisory"] is True         # 자문(키 필요)
    assert rep["aux_overrides"] == []                  # 전부 auto
    res = tune.apply(cfg)
    assert "compress_first" in res["applied"]
    assert read_config_scalar(cfg.hermes_home, ["compression", "protect_first_n"]) == "6"
    assert read_config_scalar(cfg.hermes_home, ["tools", "tool_search", "threshold_pct"]) == "25"
    # 재적용 = 멱등(이미 권장값 → skipped)
    assert tune.apply(cfg)["applied"] == []
    # revert → 원복
    assert tune.revert(cfg) is True
    assert read_config_scalar(cfg.hermes_home, ["compression", "protect_first_n"]) == "3"


# ───────────────────────── §29.4 Alphred-side MoA ─────────────────────────

def test_parse_moa_filters():
    assert classifier.parse_moa("") is None
    assert classifier.parse_moa("short") is None                    # 너무 짧음
    long = "A" * 200
    assert classifier.parse_moa("```\nFinal deliverable: " + long + "\n```").startswith("A")
    assert classifier.parse_moa("tiny", original="X" * 500) is None  # 과도 축약


def test_moa_refines_high_depth(tmp_path):
    store = Store(tmp_path / "q.db")
    mgr = QueueManager(store, _FakeRun(), tmp_path / "QUEUE.MD", verify=False,
                       moa=lambda req, res: "IMPROVED:" + res)
    t = mgr.submit("심층 분석 리포트", kind="heavy", priority=5, depth="high")
    for _ in range(3):    # 단일슬롯: 시작 tick → 마감 tick
        mgr.tick()
    done = mgr.get(t.id)
    assert done.state == TaskState.COMPLETED.value
    assert (done.result or "").startswith("IMPROVED:")


def test_moa_skipped_on_non_high(tmp_path):
    store = Store(tmp_path / "q.db")
    mgr = QueueManager(store, _FakeRun(), tmp_path / "QUEUE.MD", verify=False,
                       moa=lambda req, res: "IMPROVED:" + res)
    t = mgr.submit("간단 작업", kind="heavy", priority=5, depth="mid")
    for _ in range(3):
        mgr.tick()
    assert not (mgr.get(t.id).result or "").startswith("IMPROVED:")
