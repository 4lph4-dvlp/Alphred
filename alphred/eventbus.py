"""실행 이벤트 인프로세스 팬아웃 버스(§33) — Heavy run 라이브 스트리밍용.

Hermes `/v1/runs/{id}/events` 는 **단일 소비자**(run 당 큐 하나를 drain)라 두 곳이 동시에
구독할 수 없다. 그래서 백그라운드 진행 추적기(`queue_manager._track_run`)가 그 SSE 를 **한 번만**
소비하면서 파싱한 이벤트를 이 버스로 publish 하고, 게이트웨이의 라이브 스트림 엔드포인트가
버스를 subscribe 해 TUI 로 팬아웃한다. 진행 추적기는 데몬 스레드, 구독자는 asyncio 루프이므로
`loop.call_soon_threadsafe` 로 스레드→루프 브리지한다.
"""
from __future__ import annotations

import asyncio
import threading


class RunEventBus:
    def __init__(self, maxsize: int = 2000):
        # task_id -> list of (loop, asyncio.Queue)
        self._subs: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self, run_key: str) -> asyncio.Queue:
        """현재 asyncio 루프에서 호출 — 이 run 의 이벤트를 받을 큐를 반환."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.setdefault(run_key, []).append((loop, q))
        return q

    def unsubscribe(self, run_key: str, q: asyncio.Queue) -> None:
        with self._lock:
            lst = self._subs.get(run_key)
            if not lst:
                return
            kept = [(loop, qq) for (loop, qq) in lst if qq is not q]
            if kept:
                self._subs[run_key] = kept
            else:
                self._subs.pop(run_key, None)

    def has_subscribers(self, run_key: str) -> bool:
        with self._lock:
            return bool(self._subs.get(run_key))

    def publish(self, run_key: str, event) -> None:
        """스레드에서 호출 가능 — 각 구독자 루프로 이벤트를 안전하게 전달(느린 구독자는 드롭)."""
        with self._lock:
            subs = list(self._subs.get(run_key, ()))
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

    def close(self, run_key: str) -> None:
        """run 종료 — 구독자에게 종료 센티널(None)을 보낸다."""
        self.publish(run_key, None)
