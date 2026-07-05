"""§38 D/E/F: 프로바이더 예산 관리 및 적응(AIMD) 기능."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("alphred.budget")

# 프로바이더별 기본 예산 한도 (NIM 40 RPM, OpenRouter 20 RPM/50 RPD)
BUDGET_DEFAULTS = {
    "nvidia": {"rpm": 40.0, "rpd": float("inf"), "est_run_rpm": 12.0},
    "openrouter": {"rpm": 20.0, "rpd": 50.0, "est_run_rpm": 12.0},
    "hermes": {"rpm": 60.0, "rpd": float("inf"), "est_run_rpm": 12.0},
}


def get_provider_budget(provider: str) -> dict:
    """프로바이더별 RPM, RPD, est_run_rpm 설정을 반환한다. 환경변수 오버라이드 지원."""
    prov = provider.lower()
    base = BUDGET_DEFAULTS.get(prov, BUDGET_DEFAULTS["hermes"]).copy()

    # Env overrides: ALPHRED_BUDGET_{PROVIDER}_RPM 등
    env_rpm = os.environ.get(f"ALPHRED_BUDGET_{prov.upper()}_RPM")
    if env_rpm:
        try:
            base["rpm"] = float(env_rpm)
        except ValueError:
            pass

    env_rpd = os.environ.get(f"ALPHRED_BUDGET_{prov.upper()}_RPD")
    if env_rpd:
        try:
            base["rpd"] = float(env_rpd) if env_rpd.lower() not in ("inf", "infinity") else float("inf")
        except ValueError:
            pass

    env_est = os.environ.get(f"ALPHRED_BUDGET_{prov.upper()}_EST_RUN_RPM")
    if env_est:
        try:
            base["est_run_rpm"] = float(env_est)
        except ValueError:
            pass

    return base


# ---- §38 P3: RPD (Requests Per Day) 원장 관리 ----

def _get_ledger_path(alphred_home: Path) -> Path:
    return Path(alphred_home) / "budget_ledger.json"


def _read_ledger(alphred_home: Path) -> dict:
    p = _get_ledger_path(alphred_home)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_ledger(alphred_home: Path, data: dict) -> None:
    p = _get_ledger_path(alphred_home)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("budget_ledger.json 쓰기 실패: %s", e)


def record_request(alphred_home: Path, provider: str) -> None:
    """오늘 날짜 기준 해당 프로바이더의 요청 횟수를 1 증가시킨다."""
    prov = provider.lower()
    today = datetime.now(timezone.utc).date().isoformat()
    data = _read_ledger(alphred_home)

    if today not in data:
        data[today] = {}
    data[today][prov] = data[today].get(prov, 0) + 1

    _write_ledger(alphred_home, data)
    logger.info("budget request recorded for %s: %d requests today", prov, data[today][prov])


def check_rpd_limit(alphred_home: Path, provider: str) -> bool:
    """오늘 요청 횟수가 프로바이더 RPD 한도를 초과했는지 확인한다."""
    prov = provider.lower()
    budget = get_provider_budget(prov)
    rpd_limit = budget.get("rpd", float("inf"))
    if rpd_limit == float("inf"):
        return False

    today = datetime.now(timezone.utc).date().isoformat()
    data = _read_ledger(alphred_home)
    current = data.get(today, {}).get(prov, 0)

    if current >= rpd_limit:
        logger.warning("%s 프로바이더 오늘 RPD 한도 도달 (%d/%d)", prov, current, rpd_limit)
        return True
    return False


# ---- §38 P3: AIMD (Additive Increase/Multiplicative Decrease) 동적 슬롯 제어 ----

def _get_caps_path(alphred_home: Path) -> Path:
    return Path(alphred_home) / "provider_capacities.json"


def _read_capacities(alphred_home: Path) -> dict:
    p = _get_caps_path(alphred_home)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_capacities(alphred_home: Path, data: dict) -> None:
    p = _get_caps_path(alphred_home)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("provider_capacities.json 쓰기 실패: %s", e)


def get_current_capacity(alphred_home: Path, provider: str) -> int:
    """프로바이더의 현재 동적 슬롯 한도를 반환한다 (AIMD 반영)."""
    prov = provider.lower()
    budget = get_provider_budget(prov)
    max_cap = max(1, int(budget["rpm"] // budget["est_run_rpm"]))

    caps = _read_capacities(alphred_home)
    if prov in caps:
        return max(1, min(max_cap, caps[prov].get("current_cap", max_cap)))
    return max_cap


def decrease_capacity(alphred_home: Path, provider: str) -> int:
    """AIMD: 429 오류 발생 시 프로바이더 슬롯 한도를 곱으로 감소시킨다."""
    prov = provider.lower()
    current = get_current_capacity(alphred_home, prov)
    # 곱 감소: 절반으로 감소 (최소 1)
    new_cap = max(1, current // 2)

    caps = _read_capacities(alphred_home)
    caps[prov] = {
        "current_cap": new_cap,
        "last_decrease": datetime.now(timezone.utc).isoformat()
    }
    _write_capacities(alphred_home, caps)
    logger.warning("AIMD decrease for %s capacity: %d -> %d", prov, current, new_cap)
    return new_cap


def increase_capacity(alphred_home: Path, provider: str) -> int:
    """AIMD: 성공 시 프로바이더 슬롯 한도를 합으로 증가시킨다."""
    prov = provider.lower()
    budget = get_provider_budget(prov)
    max_cap = max(1, int(budget["rpm"] // budget["est_run_rpm"]))

    current = get_current_capacity(alphred_home, prov)
    if current >= max_cap:
        return max_cap

    # 합 증가: +1
    new_cap = min(max_cap, current + 1)

    caps = _read_capacities(alphred_home)
    caps[prov] = {
        "current_cap": new_cap,
        "last_increase": datetime.now(timezone.utc).isoformat()
    }
    _write_capacities(alphred_home, caps)
    logger.info("AIMD increase for %s capacity: %d -> %d", prov, current, new_cap)
    return new_cap
