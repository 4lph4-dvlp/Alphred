"""안전망 테스트 (#30719) — 페이로드 필터 + 재시작 폭주 가드."""
from __future__ import annotations

import pytest

from alphred.db import Store, new_id
from alphred.models import TaskState
from alphred.queue_manager import QueueManager
from alphred.safety import BlockedPayloadError, RestartGuard, scan_payload


# ---- 페이로드 필터 ----
@pytest.mark.parametrize("text", [
    "hermes gateway restart now",
    "please run systemctl restart hermes",
    "sudo reboot",
    "launchctl bootout gui/501/hermes",
    "taskkill /F /IM hermes.exe gateway",
    "pkill -f hermes-gateway",
])
def test_scan_blocks_lifecycle(text):
    assert scan_payload(text) is not None


@pytest.mark.parametrize("text", [
    "오늘 날씨 알려줘",
    "restart my motivation",          # 'restart' 단독은 명령 패턴 아님
    "summarize the meeting notes",
    "analyze the database",
])
def test_scan_allows_normal(text):
    assert scan_payload(text) is None


class FakeClient:
    def start_run(self, p, **k): return "run_" + new_id()[:6]
    def get_run(self, r): return {"status": "running"}
    def stop_run(self, r): return {"status": "stopping"}
    def close(self): pass


def make(tmp_path):
    return QueueManager(Store(tmp_path / "q.db"), FakeClient(), tmp_path / "Q.MD")


def test_submit_blocks_lifecycle_payload(tmp_path):
    mgr = make(tmp_path)
    with pytest.raises(BlockedPayloadError):
        mgr.submit("hermes gateway restart", priority=5)
    assert mgr.list() == []          # 큐에 들어가지 않음


def test_submit_allows_normal_payload(tmp_path):
    mgr = make(tmp_path)
    t = mgr.submit("일반 작업", priority=5)
    assert t.state == TaskState.PENDING.value


# ---- 재시작 폭주 가드 ----
def test_guard_trips_after_threshold(tmp_path):
    g = RestartGuard(tmp_path / "r.json", window_seconds=60, threshold=3)
    base = 1000.0
    assert g.record_restart(base) == 1
    assert g.record_restart(base + 1) == 2
    assert not g.tripped(base + 1)
    assert g.record_restart(base + 2) == 3
    assert g.tripped(base + 2)


def test_guard_window_expiry(tmp_path):
    g = RestartGuard(tmp_path / "r.json", window_seconds=60, threshold=3)
    g.record_restart(1000.0)
    g.record_restart(1001.0)
    # 70초 뒤 재시작 → 앞의 두 건은 윈도우 밖
    assert g.record_restart(1071.0) == 1
    assert not g.tripped(1071.0)


def test_guard_reset(tmp_path):
    g = RestartGuard(tmp_path / "r.json", window_seconds=60, threshold=2)
    g.record_restart(1.0); g.record_restart(2.0)
    assert g.tripped(2.0)
    g.reset()
    assert g.count(2.0) == 0


def test_halt_stops_scheduler(tmp_path):
    mgr = make(tmp_path)
    mgr.submit("작업", priority=5)
    mgr.set_halted(True, "test")
    mgr.tick()
    # halt 중에는 시작되지 않음
    assert mgr.get(mgr.list()[0].id).state == TaskState.PENDING.value
    mgr.set_halted(False)
    mgr.tick()
    assert mgr.list()[0].state == TaskState.IN_PROGRESS.value
