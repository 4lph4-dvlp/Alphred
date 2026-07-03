"""Hermes OpenAI 호환 API(:8642) 클라이언트.

Phase 0 PoC 에서 검증한 엔드포인트만 사용한다:
  POST /v1/runs                 — 비동기 실행, run_id 즉시 반환
  GET  /v1/runs/{id}            — 상태 폴링 (진행률; /events 는 불안정하여 폴링 사용)
  POST /v1/runs/{id}/stop       — 중단 (선점)
  POST /v1/responses            — previous_response_id 재개 / 동기 응답
"""
from __future__ import annotations

from typing import Any

import httpx

# Hermes run status 문자열을 Alphred 가 다루는 결과(outcome)로 정규화한다.
# 여러 곳(스케줄러 마감/크래시 복구)에서 같은 매핑을 쓰므로 한 곳에 모은다.
_RUN_DONE = {"completed", "succeeded"}
_RUN_FAILED = {"failed", "error"}
_RUN_CANCELLED = {"cancelled", "canceled", "stopped", "interrupted"}
TERMINAL_RUN = _RUN_DONE | _RUN_FAILED | _RUN_CANCELLED


def run_outcome(status: str | None) -> str:
    """run status → "done" | "failed" | "cancelled" | "running" | "unknown"."""
    s = (status or "").lower()
    if s in _RUN_DONE:
        return "done"
    if s in _RUN_FAILED:
        return "failed"
    if s in _RUN_CANCELLED:
        return "cancelled"
    if s == "running":
        return "running"
    return "unknown"


class HermesClient:
    def __init__(self, base_url: str, api_key: str | None, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # ---- runs (비동기, Heavy 작업) ----
    def start_run(
        self,
        prompt: str,
        *,
        conversation_history: list[dict] | None = None,
        previous_response_id: str | None = None,
        session_id: str | None = None,
        model: str = "hermes-agent",
    ) -> str:
        """비동기 run 시작 → run_id 반환."""
        body: dict[str, Any] = {"model": model, "input": prompt}
        if conversation_history:
            body["conversation_history"] = conversation_history
        elif previous_response_id:
            body["previous_response_id"] = previous_response_id
        if session_id:
            body["session_id"] = session_id
        r = self._http.post("/runs", json=body)
        r.raise_for_status()
        data = r.json()
        return data.get("run_id") or data.get("id")

    def get_run(self, run_id: str) -> dict:
        r = self._http.get(f"/runs/{run_id}")
        r.raise_for_status()
        return r.json()

    def stop_run(self, run_id: str) -> dict:
        r = self._http.post(f"/runs/{run_id}/stop", json={})
        r.raise_for_status()
        return r.json()

    # ---- responses (동기/재개) ----
    def respond_passthrough(self, body: dict) -> dict:
        """원본 요청 본문을 그대로 /responses 로 프록시(멀티모달 input·옵션 보존).

        Light 경로에서 chat/completions 와 대칭으로 멀티모달 페이로드를 유실 없이 전달한다.
        """
        payload = {"model": "hermes-agent", **body}
        r = self._http.post("/responses", json=payload)
        r.raise_for_status()
        return r.json()

    def chat_completion(self, body: dict) -> dict:
        r = self._http.post("/chat/completions", json=body)
        r.raise_for_status()
        return r.json()

    def models(self) -> dict:
        r = self._http.get("/models")
        r.raise_for_status()
        return r.json()

    def skills(self) -> dict:
        """설치된 스킬 목록(:8642 /v1/skills) — API 서버 에이전트가 보는 것과 동일."""
        r = self._http.get("/skills")
        r.raise_for_status()
        return r.json()

    def toolsets(self) -> dict:
        """활성 툴셋/도구 목록(:8642 /v1/toolsets) — 능력 레지스트리(§34.5)가 소비."""
        r = self._http.get("/toolsets")
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = self._http.get("/models")
            return r.status_code == 200
        except Exception:
            return False
