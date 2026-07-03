"""§35.1 M6-R1 테스트 — 클라이언트 키·스코프 인증·바인딩 안전장치."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

from alphred import clientkeys
from alphred.db import Store
from alphred.gateway import check_bind_safety, create_app
from alphred.queue_manager import QueueManager


# ---- clientkeys ----
def test_issue_verify_revoke_roundtrip(tmp_path):
    key = clientkeys.issue(tmp_path, "노트북", "control")
    assert key.startswith("alph_") and len(key) > 20
    assert clientkeys.verify(tmp_path, key) == "control"
    assert clientkeys.verify(tmp_path, "wrong") is None
    rows = clientkeys.list_keys(tmp_path)
    assert rows[0]["name"] == "노트북" and "hash" not in rows[0]  # 평문/해시 미노출
    # 파일에는 평문이 없다
    raw = (tmp_path / "clients.json").read_text(encoding="utf-8")
    assert key not in raw
    assert clientkeys.revoke(tmp_path, "노트북")
    assert clientkeys.verify(tmp_path, key) is None               # 즉시 무효(QA-35.3)
    assert not clientkeys.revoke(tmp_path, "노트북")


def test_issue_duplicate_and_bad_scope(tmp_path):
    clientkeys.issue(tmp_path, "web", "read")
    with pytest.raises(ValueError):
        clientkeys.issue(tmp_path, "web")
    with pytest.raises(ValueError):
        clientkeys.issue(tmp_path, "x", "admin")


def test_verify_updates_last_seen(tmp_path):
    key = clientkeys.issue(tmp_path, "esp32", "read")
    assert clientkeys.list_keys(tmp_path)[0]["last_seen"] is None
    clientkeys.verify(tmp_path, key)
    assert clientkeys.list_keys(tmp_path)[0]["last_seen"]


# ---- 게이트웨이 스코프 인증 ----
class _Client:
    def start_run(self, prompt, **kw):
        return "run_x"

    def get_run(self, rid):
        return {"status": "running"}

    def chat_completion(self, body):
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    def models(self):
        return {"object": "list", "data": []}

    def close(self):
        pass


@pytest.fixture
def keyed_app(tmp_path, monkeypatch):
    """클라이언트 키(read/control)가 발급된 앱 — cfg.alphred_home 이 tmp 를 가리키게."""
    read_key = clientkeys.issue(tmp_path, "monitor", "read")
    ctl_key = clientkeys.issue(tmp_path, "laptop", "control")
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    monkeypatch.delenv("ALPHRED_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    store = Store(tmp_path / "g.db")
    mgr = QueueManager(store, _Client(), tmp_path / "QUEUE.MD")
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        yield tc, read_key, ctl_key


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def test_scope_enforcement(keyed_app):
    tc, read_key, ctl_key = keyed_app
    # 무키 → 401 (키가 하나라도 있으면 인증 필수)
    assert tc.get("/queue").status_code == 401
    # read 키: GET 허용, 변경류 403 (QA-35.3)
    assert tc.get("/queue", headers=_h(read_key)).status_code == 200
    r = tc.post("/v1/chat/completions", headers=_h(read_key),
                json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    # control 키: 전부 허용
    assert tc.get("/queue", headers=_h(ctl_key)).status_code == 200
    r2 = tc.post("/v1/chat/completions", headers=_h(ctl_key),
                 json={"messages": [{"role": "user", "content": "hi"}]})
    assert r2.status_code == 200
    # 대시보드 페이지 자체는 무인증(JS 가 키 전송) — 현행 유지
    assert tc.get("/").status_code == 200


def test_legacy_single_key_is_control(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    monkeypatch.setenv("ALPHRED_API_KEY", "legacy-secret")
    store = Store(tmp_path / "g.db")
    mgr = QueueManager(store, _Client(), tmp_path / "QUEUE.MD")
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        assert tc.get("/queue").status_code == 401
        assert tc.get("/queue", headers=_h("legacy-secret")).status_code == 200
        r = tc.post("/v1/chat/completions", headers=_h("legacy-secret"),
                    json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200                    # 레거시 키 = control(하위호환)


def test_dev_mode_no_keys_open(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    monkeypatch.delenv("ALPHRED_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    store = Store(tmp_path / "g.db")
    mgr = QueueManager(store, _Client(), tmp_path / "QUEUE.MD")
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        assert tc.get("/queue").status_code == 200     # 키 전무 = 개발 모드(현행 유지)


# ---- 바인딩 안전장치 (QA-35.2) ----
def _cfg(tmp_path, api_key=None):
    return SimpleNamespace(alphred_home=tmp_path, api_key=api_key)


def test_bind_safety(tmp_path):
    cfg = _cfg(tmp_path)
    assert check_bind_safety(cfg, "127.0.0.1") is None            # 루프백 = 항상 OK
    assert check_bind_safety(cfg, "localhost") is None
    refuse = check_bind_safety(cfg, "0.0.0.0")                    # 외부 + 무인증 = 거부
    assert refuse and "keys issue" in refuse
    assert check_bind_safety(_cfg(tmp_path, "k"), "0.0.0.0") is None   # 레거시 키 OK
    clientkeys.issue(tmp_path, "dev", "control")
    assert check_bind_safety(_cfg(tmp_path), "0.0.0.0") is None   # 클라이언트 키 OK