"""클라이언트(디바이스) 키 관리 (§35.1 G1-A) — 1인 사용자 × 다기기 접속용.

기기당 키 1개 발급을 권장한다(회수 단위 = 기기). 평문 키는 발급 순간 한 번만 표시되고,
저장소(`ALPHRED_HOME/clients.json`)에는 **sha256 해시만** 보관한다(유출 내성).

스코프:
  · read    — 모니터링 전용(GET 만: 큐 조회·대시보드 폴링). 제출/조작 불가.
  · control — 전부(제출·답변·큐 조작 포함). 레거시 단일 키(API_SERVER_KEY/ALPHRED_API_KEY)
              는 control 로 간주한다(하위호환).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

CLIENTS_FILENAME = "clients.json"
SCOPES = ("read", "control")
_KEY_PREFIX = "alph_"

_lock = threading.Lock()


def _path(alphred_home) -> Path:
    return Path(alphred_home) / CLIENTS_FILENAME


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _load(alphred_home) -> list[dict]:
    try:
        data = json.loads(_path(alphred_home).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(alphred_home, entries: list[dict]) -> None:
    p = _path(alphred_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


def issue(alphred_home, name: str, scope: str = "control") -> str:
    """새 클라이언트 키 발급 — 평문 키를 반환(이후 다시 볼 수 없음). 이름은 고유해야 한다."""
    name = (name or "").strip()
    if not name:
        raise ValueError("키 이름이 필요합니다 (예: 노트북, esp32-거실)")
    if scope not in SCOPES:
        raise ValueError(f"scope 는 {SCOPES} 중 하나여야 합니다")
    with _lock:
        entries = _load(alphred_home)
        if any(e.get("name") == name for e in entries):
            raise ValueError(f"이미 존재하는 키 이름: {name!r} (revoke 후 재발급)")
        key = _KEY_PREFIX + secrets.token_hex(20)
        entries.append({"name": name, "hash": _hash(key), "scope": scope,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "last_seen": None})
        _save(alphred_home, entries)
    return key


def revoke(alphred_home, name: str) -> bool:
    """이름으로 키 회수 — 즉시 무효화. 존재했으면 True."""
    with _lock:
        entries = _load(alphred_home)
        kept = [e for e in entries if e.get("name") != name]
        if len(kept) == len(entries):
            return False
        _save(alphred_home, kept)
        return True


def list_keys(alphred_home) -> list[dict]:
    """키 목록(평문 없음) — name/scope/created_at/last_seen."""
    return [{k: e.get(k) for k in ("name", "scope", "created_at", "last_seen")}
            for e in _load(alphred_home)]


def any_keys(alphred_home) -> bool:
    return bool(_load(alphred_home))


def verify(alphred_home, presented: str | None) -> str | None:
    """제시된 키 검증 → 스코프("read"|"control") | None(불일치).

    일치 시 last_seen 을 갱신한다(분 단위 스로틀, 실패 무시 — 검증 자체는 항상 동작).
    """
    if not presented:
        return None
    h = _hash(presented)
    entries = _load(alphred_home)
    for e in entries:
        if e.get("hash") == h:
            scope = e.get("scope") if e.get("scope") in SCOPES else "control"
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            if (e.get("last_seen") or "")[:16] != now[:16]:   # 분 단위 스로틀
                try:
                    with _lock:
                        cur = _load(alphred_home)
                        for c in cur:
                            if c.get("hash") == h:
                                c["last_seen"] = now
                        _save(alphred_home, cur)
                except Exception:
                    pass
            return scope
    return None
