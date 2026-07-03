"""설정 — Hermes home/바이너리/ API 엔드포인트 해석.

QA-1.2 / QA-1.5 충족: Hermes의 home 해석 규칙(HERMES_HOME → 플랫폼 기본값)을
그대로 재사용한다. Alphred는 별도 home을 만들지 않고 Hermes home 아래에
`alphred/` 서브디렉터리만 둔다.
"""
from __future__ import annotations

import json
import os
import re
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


# ---- Hermes config.yaml 의 model 블록 읽기/쓰기 ----
# config.yaml 은 주석/순서를 보존해야 하므로 전체 YAML 라운드트립 대신 라인 단위로
# model 블록의 스칼라 키만 다룬다(gateway·tui 공용).
_MODEL_SCALARS = ("default", "provider", "base_url")


def model_config_path(hermes_home: Path) -> Path:
    return Path(hermes_home) / "config.yaml"


def read_model_config(hermes_home: Path) -> dict:
    """config.yaml 의 model 블록에서 스칼라 키(default/provider/base_url)를 읽는다."""
    out: dict = {}
    p = model_config_path(hermes_home)
    if not p.exists():
        return out
    in_model = False
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln[:1] not in (" ", "\t") and ":" in ln:
            in_model = ln.startswith("model:")
            continue
        if in_model:
            m = re.match(r"\s+(\w+):\s*(.+?)\s*$", ln)
            if m and m.group(1) in _MODEL_SCALARS:
                out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def read_default_model(hermes_home: Path) -> str | None:
    """config.yaml 의 model.default 값을 읽는다(현재 모델)."""
    return read_model_config(hermes_home).get("default")


def set_model_fields(hermes_home: Path, *, default: str | None = None,
                     provider: str | None = None, base_url: str | None = None) -> bool:
    """config.yaml 의 model 블록 스칼라 키(default/provider/base_url)를 라인편집으로 교체.

    멱등: 현재 값과 같으면 파일을 쓰지 않는다(불필요한 mtime 변경 = Hermes config 캐시 무효화
    방지). 하나라도 실제로 바뀌면 True. 코어 무수정 — 주석/순서 보존 라인편집.
    """
    fields = {k: v for k, v in (("default", default), ("provider", provider),
                                ("base_url", base_url)) if v is not None}
    if not fields:
        return False
    cur = read_model_config(hermes_home)
    if all(str(cur.get(k)) == str(v) for k, v in fields.items()):
        return False  # 변경 없음 → 미기록(캐시 유지)
    try:
        p = model_config_path(hermes_home)
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        in_model, done, out = False, set(), []
        for ln in lines:
            if ln[:1] not in (" ", "\t") and ":" in ln:
                in_model = ln.startswith("model:")
            if in_model:
                m = re.match(r"^(\s+)(\w+):\s*.*$", ln)
                if m and m.group(2) in fields and m.group(2) not in done:
                    key = m.group(2)
                    out.append(f"{m.group(1)}{key}: {fields[key]}\n")
                    done.add(key)
                    continue
            out.append(ln)
        if done:
            p.write_text("".join(out), encoding="utf-8")
        return bool(done)
    except Exception:
        return False


def set_model_default(hermes_home: Path, name: str) -> bool:
    """config.yaml 의 model.default 라인을 교체한다(set_model_fields 얇은 래퍼, 하위호환)."""
    return set_model_fields(hermes_home, default=name)


# ---- depth별 모델 라우팅 (§29.1) ----
# Alphred 가 작업 깊이(High/Mid/Light)별로 다른 모델을 쓰도록 사용자 설정을 보관/해석한다.
# 영속 파일 ALPHRED_HOME/models.json: {"high":<spec>, "mid":<spec>, "low":<spec>, "base":<name>}
#   spec = 모델 이름 문자열 | {"model":..., "provider"?:..., "base_url"?:...}(크로스 프로바이더용)
#   base = tier 미설정 depth 에서 복원할 사용자 기본 모델(첫 tier 설정 시 1회 스냅샷).
MODELS_FILENAME = "models.json"
# tier 이름 = 작업 심화도와 동일(high/mid/low). 작업 무게 Heavy/Light 와의 혼동을 피하려고
# light 가 아니라 low 를 쓴다(동기 Light 종류 요청은 low tier 로 매핑).
_TIERS = ("high", "mid", "low")


def _tier_for_depth(depth: str | None) -> str:
    """작업 심화도(low/mid/high) → 모델 tier. 동일 이름(미지정/동기 Light → low)."""
    return depth if depth in _TIERS else "low"


def read_models_file(alphred_home: Path) -> dict:
    try:
        return json.loads((Path(alphred_home) / MODELS_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _norm_spec(spec) -> dict | None:
    if isinstance(spec, str) and spec.strip():
        return {"model": spec.strip()}
    if isinstance(spec, dict) and spec.get("model"):
        return {k: v for k, v in spec.items()
                if k in ("model", "provider", "base_url") and v}
    return None


# ---- 중첩 블록 스칼라 read/set (§29.3 alphred tune) ----
# config.yaml 은 주석/순서 보존이 중요하므로 전체 YAML 라운드트립 대신, 블록 스타일
# 파일에서 keys 경로(예 ["tools","tool_search","threshold_pct"])의 스칼라 라인만 다룬다.

def _config_lines(hermes_home: Path) -> list[str]:
    return model_config_path(hermes_home).read_text(encoding="utf-8").splitlines(keepends=True)


def _find_scalar(lines: list[str], keys: list[str]):
    """블록 스타일 YAML 에서 keys 경로의 스칼라 라인을 찾는다 → (idx, indent, 현재값) | None."""
    depth = 0
    anc: list[int] = []          # 매칭된 조상들의 들여쓰기 스택(len==depth)
    for i, ln in enumerate(lines):
        s = ln.rstrip("\n")
        stripped = s.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(s) - len(stripped)
        m = re.match(r"([\w\-]+):(.*)$", stripped)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        while anc and indent <= anc[-1]:   # 블록을 벗어났으면 조상 pop
            anc.pop()
            depth -= 1
        ok_indent = (indent == 0) if depth == 0 else (bool(anc) and indent > anc[-1])
        if depth < len(keys) and key == keys[depth] and ok_indent:
            if depth == len(keys) - 1:
                return i, indent, rest.strip()
            anc.append(indent)
            depth += 1
    return None


def read_config_scalar(hermes_home: Path, keys: list[str]) -> str | None:
    """config.yaml 의 중첩 스칼라 값(따옴표 제거)을 읽는다. 없으면 None."""
    try:
        found = _find_scalar(_config_lines(hermes_home), keys)
    except Exception:
        return None
    return found[2].strip().strip("\"'") if found else None


def set_config_scalar(hermes_home: Path, keys: list[str], value) -> bool:
    """config.yaml 의 중첩 스칼라를 라인편집으로 교체. 멱등(동일값이면 미기록). 코어 무수정."""
    try:
        lines = _config_lines(hermes_home)
    except Exception:
        return False
    found = _find_scalar(lines, keys)
    if found is None:
        return False
    idx, indent, cur = found
    new_val = str(value)
    if cur.strip().strip("\"'") == new_val:
        return False
    lines[idx] = " " * indent + f"{keys[-1]}: {new_val}\n"
    try:
        model_config_path(hermes_home).write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


# ---- §35.4 프로파일 — env 5종 조합 대신 한 단어 프리셋(제품 UX) ----
# basic = 큐/선점/검증만(§34 이전 기본값과 동일). smart = +IntentCard+플래너(라이브 검증 완료,
# 질문 없음 — 마찰 0). full = +인테이크 질문+오케스트레이션+watchdog(§34 전체).
# 해석 순서: env ALPHRED_PROFILE > 파일 ALPHRED_HOME/profile > basic. 개별 env 는 항상 우선.
PROFILES = ("basic", "smart", "full")
PROFILE_FILENAME = "profile"


def read_profile(alphred_home: Path) -> str | None:
    try:
        v = (Path(alphred_home) / PROFILE_FILENAME).read_text(encoding="utf-8").strip().lower()
        return v if v in PROFILES else None
    except Exception:
        return None


def set_profile(alphred_home: Path, name: str) -> None:
    name = (name or "").strip().lower()
    if name not in PROFILES:
        raise ValueError(f"profile 은 {PROFILES} 중 하나여야 합니다")
    p = Path(alphred_home) / PROFILE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(name + "\n", encoding="utf-8")


def _flag(name: str, default: bool) -> bool:
    """env 가 비어 있으면 default(프로파일 기본값), 있으면 env 가 최종 결정."""
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


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
    client_timeout: float = 300.0         # Hermes(:8642) HTTP 클라이언트 타임아웃(긴 도구작업·설치 대비)
    stream_read_timeout: float = 600.0    # Alphred가 띄운 Hermes의 LLM 스트림 읽기(토큰간) 타임아웃 상향
                                          #   (기본 120s → 느린 free-tier 70B 등의 "Request timed out." 완화)
    restart_window_seconds: float = 60.0
    restart_threshold: int = 3
    llm_classify: bool = False
    planner: bool = False                 # §19: 계획기반 분류(모호 입력만 LLM 분해)
    verify: bool = True                   # §21: 완료 산출물 Tier0 결정적 검증(무비용, 기본 on)
    judge: bool = False                   # §21 Tier2: LLM-judge 수용검증(쿼터 사용 → 기본 off)
    judge_max_retries: int = 2            # §21 Tier3: judge 미통과 시 폐루프 재시도 상한
    rank: bool = True                     # §22 큐 상대 우선순위 재정렬(경쟁 시에만 LLM 호출 → 기본 on)
    autonomous_exec: bool = True          # §28 자율 실행: Alphred가 띄운 Hermes 게이트웨이에 YOLO 주입
                                          #     (백그라운드 run 의 execute_code/위험명령 승인대기→차단 해소)
    light_harness: bool = True            # §29.2 Light(즉답) 시스템 메시지 주입(콜드스타트 해소, 기본 on)
    # §29.1 depth별 모델 라우팅 — 환경변수 오버라이드(없으면 models.json → base). 기본 미설정.
    model_high: str | None = None         # ALPHRED_MODEL_HIGH
    model_mid: str | None = None          # ALPHRED_MODEL_MID
    model_low: str | None = None          # ALPHRED_MODEL_LOW
    moa: bool = False                     # §29.4 Alphred-side MoA(high 한정, 비용 → 기본 off)
    moa_samples: int = 2                  # §29.4 Mode B 표본 수 상한(예산)
    caps: bool = True                     # §34.5 능력 레지스트리 → 동적 하네스 주입(무LLM, 기본 on)
    caps_ttl: float = 3600.0              # §34.5 능력 스냅샷 캐시 TTL(초)
    profile: str = "basic"                # §35.4 프리셋(basic|smart|full) — 아래 §34 플래그의 기본값 결정
    intent: bool = False                  # §34.2 IntentCard(LLM-first 의도 판정, 기본 off)
    clarify: bool = False                 # §34.4 인테이크 질문+추천답변(IntentCard 필요, 기본 off)
    clarify_timeout: float = 600.0        # §34.4 답변 대기 타임아웃(초) — 경과 시 가정 기록 후 진행
    orchestrate: bool = False             # §34.6 StepRunner — high 심화도 Plan v2 를 스텝 단위 실행
    task_budget: int = 25                 # §34.6 E5 — 작업당 Hermes run 예산(초과 시 부분성공 NeedsReview)
    step_retries: int = 2                 # §34.6 E2 — 스텝 수용검사 실패 시 그 스텝만 재시도 상한
    watchdog: bool = False                # §34.6 E3 — 실행 중 감시(도구오류 루프/무진전 → 중단·교정 재개)
    stall_seconds: float = 600.0          # §34.6 E3 — 무진전 판정 기준(관측 이벤트/갱신 없음, 초)
    tool_fail_limit: int = 3              # §34.6 E3 — 연속 도구 실패 개입 임계

    @property
    def guard_path(self) -> Path:
        return self.alphred_home / "restarts.json"

    @property
    def system_prompt_path(self) -> Path:
        # 백그라운드 실행 하네스 사용자 편집본(§26). 있으면 기본 자산보다 우선.
        return self.alphred_home / "system_prompt.md"

    @property
    def preferences_path(self) -> Path:
        # §34.4 C3 — 사용자 선호(인테이크 답변 축적, 수동 편집 가능한 평문 파일).
        return self.alphred_home / "preferences.md"

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
        # §35.4 프로파일 — §34 파이프라인 플래그들의 기본값을 결정(개별 env 가 항상 우선)
        prof = (os.environ.get("ALPHRED_PROFILE", "").strip().lower()
                or read_profile(alphred_home) or "basic")
        if prof not in PROFILES:
            prof = "basic"
        smart = prof in ("smart", "full")
        full = prof == "full"
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
            client_timeout=float(os.environ.get("ALPHRED_CLIENT_TIMEOUT", "300")),
            stream_read_timeout=float(os.environ.get("ALPHRED_STREAM_READ_TIMEOUT", "600")),
            restart_window_seconds=float(os.environ.get("ALPHRED_RESTART_WINDOW", "60")),
            restart_threshold=int(os.environ.get("ALPHRED_RESTART_THRESHOLD", "3")),
            llm_classify=os.environ.get("ALPHRED_LLM_CLASSIFY", "").lower() in ("1", "true", "yes"),
            planner=_flag("ALPHRED_PLANNER", smart),   # §35.4 smart+ 기본 ON
            profile=prof,
            verify=os.environ.get("ALPHRED_VERIFY", "1").lower() not in ("0", "false", "no"),
            judge=os.environ.get("ALPHRED_JUDGE", "").lower() in ("1", "true", "yes"),
            judge_max_retries=int(os.environ.get("ALPHRED_JUDGE_RETRIES", "2")),
            rank=os.environ.get("ALPHRED_RANK", "1").lower() not in ("0", "false", "no"),
            autonomous_exec=os.environ.get("ALPHRED_AUTONOMOUS_EXEC", "1").lower()
            not in ("0", "false", "no"),
            light_harness=os.environ.get("ALPHRED_LIGHT_HARNESS", "1").lower()
            not in ("0", "false", "no"),
            model_high=os.environ.get("ALPHRED_MODEL_HIGH", "").strip() or None,
            model_mid=os.environ.get("ALPHRED_MODEL_MID", "").strip() or None,
            model_low=os.environ.get("ALPHRED_MODEL_LOW", "").strip() or None,
            moa=os.environ.get("ALPHRED_MOA", "").lower() in ("1", "true", "yes"),
            moa_samples=int(os.environ.get("ALPHRED_MOA_SAMPLES", "2")),
            caps=os.environ.get("ALPHRED_CAPS", "1").lower() not in ("0", "false", "no"),
            caps_ttl=float(os.environ.get("ALPHRED_CAPS_TTL", "3600")),
            intent=_flag("ALPHRED_INTENT", smart),         # §35.4 smart+ 기본 ON
            clarify=_flag("ALPHRED_CLARIFY", full),        # §35.4 full 기본 ON
            clarify_timeout=float(os.environ.get("ALPHRED_CLARIFY_TIMEOUT", "600")),
            orchestrate=_flag("ALPHRED_ORCHESTRATE", full),
            task_budget=int(os.environ.get("ALPHRED_TASK_BUDGET", "25")),
            step_retries=int(os.environ.get("ALPHRED_STEP_RETRIES", "2")),
            watchdog=_flag("ALPHRED_WATCHDOG", full),
            stall_seconds=float(os.environ.get("ALPHRED_STALL_SECONDS", "600")),
            tool_fail_limit=int(os.environ.get("ALPHRED_TOOL_FAIL_LIMIT", "3")),
        )

    # ---- §29.1 depth별 모델 tier 해석/설정 ----
    @property
    def models_json_path(self) -> Path:
        return self.alphred_home / MODELS_FILENAME

    def _env_tier(self, tier: str) -> str | None:
        return {"high": self.model_high, "mid": self.model_mid,
                "low": self.model_low}.get(tier)

    def has_model_tiers(self) -> bool:
        """사용자가 depth별 모델을 하나라도 설정했는가(env 또는 models.json)."""
        if any(self._env_tier(t) for t in _TIERS):
            return True
        data = read_models_file(self.alphred_home)
        return any(data.get(t) for t in _TIERS)

    def model_for_depth(self, depth: str | None) -> dict | None:
        """작업 심화도 → 명시 설정된 모델 spec {model, provider?, base_url?} | None.

        우선순위: 환경변수 > models.json tier. 어느 쪽도 없으면 None(= base 사용은 적용기가 결정).
        """
        tier = _tier_for_depth(depth)
        env_name = self._env_tier(tier)
        if env_name:
            return {"model": env_name}
        return _norm_spec(read_models_file(self.alphred_home).get(tier))

    def model_base_default(self) -> str | None:
        """tier 미설정 depth 에서 복원할 기본 모델(models.json base → config.default)."""
        return (read_models_file(self.alphred_home).get("base")
                or read_default_model(self.hermes_home))

    def get_tiers(self) -> dict:
        """현재 depth별 모델 매핑(표시/ API용) — env 우선, 출처 라벨 포함."""
        data = read_models_file(self.alphred_home)
        out: dict = {"base": data.get("base") or read_default_model(self.hermes_home)}
        for t in _TIERS:
            env_name = self._env_tier(t)
            if env_name:
                out[t] = {"model": env_name, "source": "env"}
            else:
                spec = _norm_spec(data.get(t))
                out[t] = ({**spec, "source": "models.json"} if spec else None)
        return out

    def set_tier_model(self, tier: str, spec: dict | str | None) -> None:
        """models.json 에 depth tier 모델을 영속 설정(spec=None → 해제).

        첫 tier 설정 시 현재 config.default 를 base 로 1회 스냅샷한다(미설정 depth 복원용).
        """
        if tier not in _TIERS:
            raise ValueError(f"unknown tier: {tier!r} (high|mid|low)")
        data = read_models_file(self.alphred_home)
        if "base" not in data:
            base = read_default_model(self.hermes_home)
            if base:
                data["base"] = base
        if spec is None:
            data.pop(tier, None)
        else:
            norm = _norm_spec(spec)
            if norm is None:
                raise ValueError("invalid model spec")
            data[tier] = norm["model"] if set(norm) == {"model"} else norm
        self._write_models_file(data)

    def _write_models_file(self, data: dict) -> None:
        self.models_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.models_json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_default_model(self, name: str) -> None:
        """모델을 **영구 기본값**으로 설정 — 사용자가 다시 바꾸기 전까지 유지된다.

        1) config.yaml `model.default` = name (재시작 후에도 유지)
        2) models.json base = name, **깊이별 tier(high/mid/low) 해제** → §29.1 라우팅이
           config.default 를 덮어쓰지 않도록(has_model_tiers=False → apply_model no-op).
        같은 provider 내 model-id 전환 전제(provider/base_url 은 그대로).
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("model name required")
        set_model_fields(self.hermes_home, default=name)
        data = read_models_file(self.alphred_home)
        data["base"] = name
        for t in _TIERS:
            data.pop(t, None)
        data.pop("light", None)   # 구 키(light→low 리네임 잔재) 정리
        self._write_models_file(data)
