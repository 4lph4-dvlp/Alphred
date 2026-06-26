"""설정 — Hermes home/바이너리/ API 엔드포인트 해석.

QA-1.2 / QA-1.5 충족: Hermes의 home 해석 규칙(HERMES_HOME → 플랫폼 기본값)을
그대로 재사용한다. Alphred는 별도 home을 만들지 않고 Hermes home 아래에
`alphred/` 서브디렉터리만 둔다.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def resolve_hermes_home() -> Path:
    """Hermes의 get_hermes_home() 와 동일한 규칙으로 home 디렉터리를 해석한다."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def resolve_hermes_bin() -> str | None:
    """`hermes` 실행 파일 경로를 찾는다.

    우선순위: ALPHRED_HERMES_BIN → PATH의 hermes → 알려진 Windows venv 위치.
    """
    override = os.environ.get("ALPHRED_HERMES_BIN", "").strip()
    if override:
        return override
    found = shutil.which("hermes")
    if found:
        return found
    if sys.platform == "win32":
        guess = resolve_hermes_home() / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
        if guess.exists():
            return str(guess)
        guess2 = guess.with_suffix("")  # no extension
        if guess2.exists():
            return str(guess2)
    return None


@dataclass
class Config:
    hermes_home: Path
    alphred_home: Path
    db_path: Path
    queue_md_path: Path
    hermes_bin: str | None
    api_base_url: str
    gateway_url: str
    api_key: str | None
    max_retries: int = 3
    retry_base_seconds: float = 5.0
    restart_window_seconds: float = 60.0
    restart_threshold: int = 3
    llm_classify: bool = False
    planner: bool = False                 # §19: 계획기반 분류(모호 입력만 LLM 분해)
    verify: bool = True                   # §21: 완료 산출물 Tier0 결정적 검증(무비용, 기본 on)
    judge: bool = False                   # §21 Tier2: LLM-judge 수용검증(쿼터 사용 → 기본 off)
    judge_max_retries: int = 2            # §21 Tier3: judge 미통과 시 폐루프 재시도 상한

    @property
    def guard_path(self) -> Path:
        return self.alphred_home / "restarts.json"

    @property
    def cron_jobs_path(self) -> Path:
        # Hermes cron 정의를 그대로 읽는다. 별도 지정 시 ALPHRED_CRON_JOBS.
        env = os.environ.get("ALPHRED_CRON_JOBS", "").strip()
        return Path(env) if env else (self.hermes_home / "cron" / "jobs.json")

    @property
    def cron_state_path(self) -> Path:
        return self.alphred_home / "cron_state.json"

    @classmethod
    def load(cls) -> "Config":
        hermes_home = resolve_hermes_home()
        alphred_home = Path(os.environ.get("ALPHRED_HOME", "").strip() or (hermes_home / "alphred"))
        alphred_home.mkdir(parents=True, exist_ok=True)
        return cls(
            hermes_home=hermes_home,
            alphred_home=alphred_home,
            db_path=Path(os.environ.get("ALPHRED_DB", "").strip() or (alphred_home / "alphred.db")),
            queue_md_path=Path(os.environ.get("ALPHRED_QUEUE_MD", "").strip() or (alphred_home / "QUEUE.MD")),
            hermes_bin=resolve_hermes_bin(),
            api_base_url=os.environ.get("ALPHRED_HERMES_API", "http://localhost:8642/v1").rstrip("/"),
            gateway_url=os.environ.get("ALPHRED_GATEWAY_URL", "http://localhost:8643").rstrip("/"),
            api_key=os.environ.get("API_SERVER_KEY") or os.environ.get("ALPHRED_API_KEY"),
            max_retries=int(os.environ.get("ALPHRED_MAX_RETRIES", "3")),
            retry_base_seconds=float(os.environ.get("ALPHRED_RETRY_BASE_SECONDS", "5")),
            restart_window_seconds=float(os.environ.get("ALPHRED_RESTART_WINDOW", "60")),
            restart_threshold=int(os.environ.get("ALPHRED_RESTART_THRESHOLD", "3")),
            llm_classify=os.environ.get("ALPHRED_LLM_CLASSIFY", "").lower() in ("1", "true", "yes"),
            planner=os.environ.get("ALPHRED_PLANNER", "").lower() in ("1", "true", "yes"),
            verify=os.environ.get("ALPHRED_VERIFY", "1").lower() not in ("0", "false", "no"),
            judge=os.environ.get("ALPHRED_JUDGE", "").lower() in ("1", "true", "yes"),
            judge_max_retries=int(os.environ.get("ALPHRED_JUDGE_RETRIES", "2")),
        )
