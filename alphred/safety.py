"""운영 안전망 (기획 1, 이슈 #30719 대응).

(a) 페이로드 필터: 에이전트/작업이 자신의 라이프사이클(게이트웨이 재시작 등)을
    제어하는 명령을 큐에 넣으려 하면 진입 단계에서 차단한다.
(b) 재시작 폭주 가드: 짧은 시간에 비정상 재시작이 반복되면(예: 60초 내 3회)
    자동 재개(스케줄러의 작업 시작/재개)를 비활성화해 무한 재시작 루프를 끊는다.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# 자기 수명주기를 건드리는 명령들 — 큐에 예약되면 #30719 류의 자해 루프 위험.
_LIFECYCLE = re.compile(
    r"""(?ix)
    \b(
        hermes \s+ gateway \s+ (restart|stop|start)
      | systemctl \s+ (--user \s+)? (restart|stop|start|kill)
      | launchctl \s+ (unload|load|stop|kickstart|bootout)
      | service \s+ \S+ \s+ (restart|stop)
      | (sudo \s+)? (reboot|shutdown|halt|poweroff)\b
      | pkill \s+ .*hermes
      | taskkill \s+ .* (hermes|gateway)
      | kill \s+ -9
      | hermes \s+ gateway \s+ uninstall
    )
    """
)


class BlockedPayloadError(Exception):
    """라이프사이클 제어 명령이 감지되어 큐 진입이 차단됨."""
    def __init__(self, reason: str, matched: str):
        super().__init__(reason)
        self.reason = reason
        self.matched = matched


def scan_payload(text: str | None) -> str | None:
    """위험한 라이프사이클 명령이 있으면 매칭 문자열을, 없으면 None 을 반환."""
    if not text:
        return None
    m = _LIFECYCLE.search(text)
    return m.group(0).strip() if m else None


class RestartGuard:
    """재시작 타임스탬프를 파일에 보존(재시작을 가로질러 추적)하고 폭주를 판정한다."""

    def __init__(self, path: Path, window_seconds: float = 60.0, threshold: int = 3):
        self.path = Path(path)
        self.window = window_seconds
        self.threshold = threshold

    def _load(self) -> list[float]:
        try:
            return [float(x) for x in json.loads(self.path.read_text())]
        except Exception:
            return []

    def _save(self, ts: list[float]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(ts))
        except Exception:
            pass

    def record_restart(self, now: float | None = None) -> int:
        """이번 기동을 기록하고, 윈도우 내 재시작 횟수를 반환한다."""
        now = time.time() if now is None else now
        ts = [t for t in self._load() if now - t <= self.window]
        ts.append(now)
        self._save(ts)
        return len(ts)

    def count(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return len([t for t in self._load() if now - t <= self.window])

    def tripped(self, now: float | None = None) -> bool:
        return self.count(now) >= self.threshold

    def reset(self) -> None:
        self._save([])
