"""D2 `alphred doctor` 진단 — 구조/무crash 검증(라이브 호출 없음)."""
from __future__ import annotations

from alphred.cli import _collect_doctor, _print_doctor
from alphred.config import Config


def test_doctor_collects_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    # 도달 불가 포트로 게이트웨이/Hermes 를 가리켜 graceful down 경로 확인
    monkeypatch.setenv("ALPHRED_GATEWAY_URL", "http://localhost:59998")
    monkeypatch.setenv("ALPHRED_HERMES_API", "http://localhost:59997/v1")
    cfg = Config.load()
    rep = _collect_doctor(cfg)
    assert "checks" in rep and isinstance(rep["ok"], bool)
    names = {c["name"] for c in rep["checks"]}
    assert "Hermes API (:8642)" in names
    assert "Alphred 게이트웨이 (:8643)" in names
    assert "큐" in names
    # 미응답 → 해당 체크는 실패로 표시
    down = {c["name"]: c["ok"] for c in rep["checks"]}
    assert down["Hermes API (:8642)"] is False
    assert down["Alphred 게이트웨이 (:8643)"] is False
    _print_doctor(rep)  # 렌더 무crash


def test_doctor_reports_planner_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    monkeypatch.setenv("ALPHRED_PLANNER", "1")
    cfg = Config.load()
    rep = _collect_doctor(cfg)
    planner = next(c for c in rep["checks"] if "플래너" in c["name"])
    assert planner["detail"] == "ON"


def test_doctor_verify_and_judge_flags_and_stats(tmp_path, monkeypatch):
    """§21: 검증/judge 플래그 + 검증 통계 섹션이 보고된다."""
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    monkeypatch.setenv("ALPHRED_JUDGE", "1")
    cfg = Config.load()
    rep = _collect_doctor(cfg)
    names = {c["name"] for c in rep["checks"]}
    assert any("산출물 검증" in n for n in names)
    assert any("수용 judge" in n for n in names)
    assert any("검증 통계" in n for n in names)
    assert "verify_stats" in rep
    judge = next(c for c in rep["checks"] if "judge" in c["name"])
    assert "ON" in judge["detail"]
