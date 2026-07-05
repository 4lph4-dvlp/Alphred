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
    "agent:\n  max_turns: 150\n"
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
    for k in ("ALPHRED_MODEL_HIGH", "ALPHRED_MODEL_MID", "ALPHRED_MODEL_LOW",
              "ALPHRED_REASONING_HIGH", "ALPHRED_REASONING_MID", "ALPHRED_REASONING_LOW"):
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


def test_models_tiers_reasoning_endpoints(tmp_path, monkeypatch):
    """POST /models/reasoning(전역) · /models/tiers 부분 갱신(model/reasoning 독립)."""
    from alphred.config import read_reasoning_effort
    cfg = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "g2.db")
    mgr = QueueManager(store, _FakeUp(), tmp_path / "QUEUE.MD")
    with TestClient(create_app(cfg, mgr=mgr, scheduler_interval=3600)) as tc:
        # 전역 설정 + 레벨 검증
        assert tc.post("/models/reasoning", json={"value": "ultra"}).status_code == 400
        r = tc.post("/models/reasoning", json={"value": "high"})
        assert r.status_code == 200 and r.json()["reasoning_effort"] == "high"
        assert read_reasoning_effort(cfg.hermes_home) == "high"
        assert tc.get("/models/tiers").json()["reasoning_effort"] == "high"
        # tier 부분 갱신: model 설정 후 reasoning 만 추가 → 둘 다 유지
        tc.post("/models/tiers", json={"tier": "high", "model": "nvidia/strong"})
        tc.post("/models/tiers", json={"tier": "high", "reasoning": "xhigh"})
        d = tc.get("/models/tiers").json()
        assert d["tiers"]["high"]["model"] == "nvidia/strong"
        assert d["tiers"]["high"]["reasoning"] == "xhigh"
        # model 교체에도 reasoning 보존
        tc.post("/models/tiers", json={"tier": "high", "model": "nvidia/other"})
        assert tc.get("/models/tiers").json()["tiers"]["high"]["reasoning"] == "xhigh"
        # reasoning 해제 → 모델만 남음, 이어서 model 해제 → tier 제거
        tc.post("/models/tiers", json={"tier": "high", "reasoning": None})
        assert "reasoning" not in tc.get("/models/tiers").json()["tiers"]["high"]
        tc.post("/models/tiers", json={"tier": "high", "model": None})
        d = tc.get("/models/tiers").json()
        assert d["tiers"]["high"] is None and d["enabled"] is False


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


# ───────────────────────── §29.1 확장: depth별 추론 깊이 ─────────────────────────

def test_set_reasoning_effort_insert_edit_idempotent(tmp_path, monkeypatch):
    """agent.reasoning_effort 라인편집 — 키 부재 시 삽입, 동일값 미기록, '' 복원."""
    from alphred.config import read_reasoning_effort, set_reasoning_effort
    cfg = _cfg(tmp_path, monkeypatch)
    assert read_reasoning_effort(cfg.hermes_home) is None      # 키 부재(구버전 config)
    assert set_reasoning_effort(cfg.hermes_home, "high") is True   # agent: 블록에 삽입
    assert read_reasoning_effort(cfg.hermes_home) == "high"
    assert set_reasoning_effort(cfg.hermes_home, "high") is False  # 멱등(미기록)
    assert set_reasoning_effort(cfg.hermes_home, "xhigh") is True
    assert read_reasoning_effort(cfg.hermes_home) == "xhigh"
    assert set_reasoning_effort(cfg.hermes_home, "") is True       # '' = Hermes 기본 복원
    assert read_reasoning_effort(cfg.hermes_home) == ""
    # 다른 블록/키는 무손질
    assert read_config_scalar(cfg.hermes_home, ["agent", "max_turns"]) == "150"
    assert read_config_scalar(cfg.hermes_home, ["compression", "threshold"]) == "0.5"


def test_reasoning_for_depth_precedence_and_snapshot(tmp_path, monkeypatch):
    from alphred.config import set_reasoning_effort
    # env 우선
    cfg = _cfg(tmp_path, monkeypatch, ALPHRED_REASONING_HIGH="xhigh")
    assert cfg.reasoning_for_depth("high") == "xhigh"
    assert cfg.has_reasoning_tiers() is True
    # models.json tier + 첫 설정 시 base_reasoning 스냅샷
    cfg2 = _cfg(tmp_path, monkeypatch)
    assert cfg2.has_reasoning_tiers() is False
    set_reasoning_effort(cfg2.hermes_home, "medium")             # 현재 전역값
    cfg2.set_tier_model("high", {"model": "nvidia/strong", "reasoning": "xhigh"})
    assert cfg2.reasoning_for_depth("high") == "xhigh"
    assert cfg2.reasoning_for_depth("mid") is None
    assert cfg2.reasoning_base_default() == "medium"
    tiers = cfg2.get_tiers()
    assert tiers["high"]["reasoning"] == "xhigh"
    assert tiers["base_reasoning"] == "medium"
    # reasoning 만 있는 tier 도 유효 — 단 모델 라우팅은 켜지지 않는다
    cfg2.set_tier_model("high", None)
    cfg2.set_tier_model("low", {"reasoning": "minimal"})
    assert cfg2.has_model_tiers() is False
    assert cfg2.has_reasoning_tiers() is True
    # 잘못된 레벨 거부
    with pytest.raises(ValueError):
        cfg2.set_tier_model("mid", {"reasoning": "ultra"})


def test_model_applier_restores_provider_across_tiers(tmp_path, monkeypatch):
    """크로스 프로바이더 tier 후 미설정 depth 복귀 시 provider/base_url 도 base 로 복원."""
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.set_tier_model("high", {"model": "google/gemma-4-31b-it", "provider": "google",
                                "base_url": "https://g/v1"})
    apply = make_model_applier(cfg)
    apply("high")
    assert read_default_model(cfg.hermes_home) == "google/gemma-4-31b-it"
    assert read_config_scalar(cfg.hermes_home, ["model", "provider"]) == "google"
    apply("mid")                                   # 미설정 depth → base 전체 복원
    assert read_default_model(cfg.hermes_home) == "nvidia/base-model"
    assert read_config_scalar(cfg.hermes_home, ["model", "provider"]) == "nvidia"
    assert (read_config_scalar(cfg.hermes_home, ["model", "base_url"])
            == "https://integrate.api.nvidia.com/v1")


def test_restore_base_model_on_shutdown(tmp_path, monkeypatch):
    """종료 시 복원 — tier 가 남긴 모델/provider/추론 깊이를 base 로 되돌린다."""
    from alphred.config import read_reasoning_effort, set_reasoning_effort
    from alphred.runtime import restore_base_model
    cfg = _cfg(tmp_path, monkeypatch)
    set_reasoning_effort(cfg.hermes_home, "medium")
    cfg.set_tier_model("high", {"model": "google/gemma-4-31b-it", "provider": "google",
                                "reasoning": "xhigh"})
    apply = make_model_applier(cfg)
    apply("high")
    assert read_default_model(cfg.hermes_home) == "google/gemma-4-31b-it"
    assert read_reasoning_effort(cfg.hermes_home) == "xhigh"
    restore_base_model(cfg)
    assert read_default_model(cfg.hermes_home) == "nvidia/base-model"
    assert read_config_scalar(cfg.hermes_home, ["model", "provider"]) == "nvidia"
    assert read_reasoning_effort(cfg.hermes_home) == "medium"
    # tier 미설정이면 완전 무동작
    cfg2 = _cfg(tmp_path, monkeypatch)
    restore_base_model(cfg2)
    assert read_default_model(cfg2.hermes_home) == "nvidia/base-model"


def test_model_applier_applies_reasoning_per_depth(tmp_path, monkeypatch):
    from alphred.config import read_reasoning_effort, set_reasoning_effort
    cfg = _cfg(tmp_path, monkeypatch)
    set_reasoning_effort(cfg.hermes_home, "medium")
    apply = make_model_applier(cfg)
    apply("high")                                        # tier 미설정 → 완전 무동작
    assert read_reasoning_effort(cfg.hermes_home) == "medium"
    cfg.set_tier_model("high", {"reasoning": "xhigh"})   # reasoning-only tier
    apply = make_model_applier(cfg)
    apply("high")
    assert read_reasoning_effort(cfg.hermes_home) == "xhigh"
    assert read_default_model(cfg.hermes_home) == "nvidia/base-model"  # 모델은 무손질
    apply("mid")                                         # 미설정 depth → base 복원
    assert read_reasoning_effort(cfg.hermes_home) == "medium"


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


def test_tune_get_set_arbitrary_scalar(tmp_path, monkeypatch):
    """§29.3 확장 — KNOBS 밖 임의 스칼라 get/set(백업·멱등·키 부재 거부·원복)."""
    cfg = _cfg(tmp_path, monkeypatch)
    assert tune.get_scalar(cfg, "agent.max_turns") == "150"
    assert tune.get_scalar(cfg, "agent.nope") is None
    r = tune.set_scalar(cfg, "agent.max_turns", 200)
    assert r["ok"] and r["changed"]
    assert read_config_scalar(cfg.hermes_home, ["agent", "max_turns"]) == "200"
    assert tune.set_scalar(cfg, "agent.max_turns", 200)["changed"] is False   # 멱등
    bad = tune.set_scalar(cfg, "agent.nope", 1)                # 신규 키 삽입 거부
    assert bad["ok"] is False and "없습니다" in bad["error"]
    assert tune.revert(cfg) is True                            # 백업 원복
    assert tune.get_scalar(cfg, "agent.max_turns") == "150"


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
