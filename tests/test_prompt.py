"""§26 실행 하네스(시스템 프롬프트) 로딩·주입 테스트."""
from __future__ import annotations

from alphred.db import Store
from alphred.models import TaskState
from alphred.prompt import (autonomous_input, default_prompt_text, init_user_prompt,
                            load_harness, user_prompt_path)
from alphred.queue_manager import QueueManager
from tests.test_preemption import ControlledClient


def test_default_prompt_has_marker_and_delimiter():
    h = default_prompt_text()
    assert "autonomous background task" in h.lower()   # 핵심 자율 실행 지시 포함
    assert h.rstrip().endswith("## REQUEST")           # 요청이 뒤에 붙는 구분선


def test_load_harness_prefers_user_override(tmp_path):
    assert load_harness(tmp_path) == default_prompt_text()      # 편집본 없음 → 기본
    user_prompt_path(tmp_path).write_text("MY HARNESS\n## REQUEST\n", encoding="utf-8")
    assert load_harness(tmp_path).startswith("MY HARNESS")      # 편집본 우선


def test_init_user_prompt_no_overwrite(tmp_path):
    path, wrote = init_user_prompt(tmp_path)
    assert wrote and path.exists()
    path.write_text("edited", encoding="utf-8")
    _, wrote2 = init_user_prompt(tmp_path)                      # force 없으면 보존
    assert wrote2 is False and path.read_text(encoding="utf-8") == "edited"
    _, wrote3 = init_user_prompt(tmp_path, overwrite=True)       # force → 덮어씀
    assert wrote3 is True and path.read_text(encoding="utf-8") != "edited"


def test_autonomous_input_uses_injected_harness_and_depth():
    out = autonomous_input("리포트 작성", None, None,
                           harness="HARNESS_X\n## REQUEST\n", depth="high")
    assert out.startswith("HARNESS_X")
    assert "리포트 작성" in out and "DEPTH: HIGH" in out


def test_queue_manager_injects_system_prompt_into_run(tmp_path):
    """주입된 하네스가 실제 Heavy run 입력 앞에 붙는다."""
    s = Store(tmp_path / "q.db")
    c = ControlledClient()
    mgr = QueueManager(s, c, tmp_path / "Q.MD",
                       system_prompt="CUSTOM_HARNESS\n## 작업 요청\n")
    mgr.submit("서울 날씨 분석 PDF", priority=5, kind="heavy")
    mgr.tick()
    sent = c.started[0][1]
    assert sent.startswith("CUSTOM_HARNESS") and "서울 날씨 분석 PDF" in sent
