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


# ---- §38 P1: model_routes alias(Hermes 기동 시 1회 등록 → 디스패치 레이스 제거) ----
# Hermes config.yaml: `model_routes: {alias: {model, provider?, base_url?}}`
# alias 이름은 고정(alphred-high/mid/low), 내용만 갱신. 변경 시 Hermes 재기동 필요(유휴 시).
_ROUTE_ALIASES = ("alphred-high", "alphred-mid", "alphred-low")
_ROUTE_KEY = "model_routes"

# model_routes 는 블록 스타일 YAML로 기록한다. 서버가 init 시 파싱하므로 형식 안정성이 중요.
# 예시:
#   model_routes:
#     alphred-high:
#       model: nvidia/llama-3.1-nemotron-ultra-253b-v1:free
#       provider: openrouter
#       base_url: https://openrouter.ai/api/v1
#     alphred-low:
#       model: nvidia/llama-3.3-nemotron-super-49b-v1:free


CATALOG_FILENAME = "model_catalog.json"

DEFAULT_CATALOG = {
    "version": 1,
    "updated_at": "2026-07-04T12:00:00Z",
    "free_only": False,
    "categories": {
        "coding": {
            "free": {"model": "nvidia/llama-3.1-nemotron-ultra-253b-v1:free", "provider": "openrouter"},
            "paid": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "primary": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "LMArena & OpenRouter coding benchmark"
        },
        "research": {
            "free": {"model": "nvidia/llama-3.3-nemotron-super-49b-v1:free", "provider": "openrouter"},
            "paid": {"model": "google/gemini-2.5-pro", "provider": "gemini"},
            "primary": {"model": "google/gemini-2.5-pro", "provider": "gemini"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "OpenRouter research benchmark"
        },
        "analysis": {
            "free": {"model": "nvidia/llama-3.3-nemotron-super-49b-v1:free", "provider": "openrouter"},
            "paid": {"model": "google/gemini-2.5-pro", "provider": "gemini"},
            "primary": {"model": "google/gemini-2.5-pro", "provider": "gemini"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "Data analysis capability score"
        },
        "writing": {
            "free": {"model": "nvidia/llama-3.1-nemotron-ultra-253b-v1:free", "provider": "openrouter"},
            "paid": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "primary": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "Long writing and report generation rankings"
        },
        "translation": {
            "free": {"model": "meta-llama/llama-3.1-70b-instruct:free", "provider": "openrouter"},
            "paid": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "primary": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "Multilingual benchmark score"
        },
        "creative": {
            "free": {"model": "meta-llama/llama-3.3-70b-instruct:free", "provider": "openrouter"},
            "paid": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "primary": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "Creative and brainstorming score"
        },
        "math": {
            "free": {"model": "meta-llama/llama-3.3-70b-instruct:free", "provider": "openrouter"},
            "paid": {"model": "google/gemini-2.5-pro", "provider": "gemini"},
            "primary": {"model": "google/gemini-2.5-pro", "provider": "gemini"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "Math and logical reasoning benchmark"
        },
        "agentic": {
            "free": {"model": "nvidia/llama-3.3-nemotron-super-49b-v1:free", "provider": "openrouter"},
            "paid": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "primary": {"model": "anthropic/claude-3.5-sonnet", "provider": "openrouter"},
            "fallbacks": [{"model": "hermes-agent"}],
            "evidence": "Tool use and code execution reliability"
        },
        "general": {
            "free": {"model": "hermes-agent"},
            "paid": {"model": "hermes-agent"},
            "primary": {"model": "hermes-agent"},
            "fallbacks": [],
            "evidence": "Fallback default model"
        }
    }
}


def read_catalog_file(alphred_home: Path) -> dict:
    p = Path(alphred_home) / CATALOG_FILENAME
    if not p.exists():
        return DEFAULT_CATALOG
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_CATALOG


def write_catalog_file(alphred_home: Path, data: dict) -> None:
    p = Path(alphred_home) / CATALOG_FILENAME
    if p.exists():
        bak = p.with_suffix(".json.bak")
        try:
            shutil.copy2(p, bak)
        except Exception:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_model_routes(alphred_home: Path, hermes_home: Path) -> dict[str, dict]:
    """models.json tier 설정 → model_routes alias 매핑(§38 P1).

    각 depth tier(high/mid/low)의 모델 spec 을 읽고, base(config.yaml model.default)와
    다른 것만 alias 로 등록한다. 전부 base 와 같거나 미설정이면 빈 dict(routes 불필요).
    """
    data = read_models_file(alphred_home)
    base_model = data.get("base") or read_default_model(hermes_home)
    base_spec = {"model": base_model or ""}
    if "base_provider" in data:
        base_spec["provider"] = str(data.get("base_provider") or "")
        base_spec["base_url"] = str(data.get("base_base_url") or "")

    routes: dict[str, dict] = {}
    for tier in ("high", "mid", "low"):
        spec = _norm_spec(data.get(tier))
        if not spec or not spec.get("model"):
            # 미설정 — base 로 폴백(alias 내용은 base)
            if base_model:
                routes[f"alphred-{tier}"] = {k: v for k, v in base_spec.items() if v}
            continue
        route: dict = {"model": spec["model"]}
        if spec.get("provider"):
            route["provider"] = spec["provider"]
        if spec.get("base_url"):
            route["base_url"] = spec["base_url"]
        routes[f"alphred-{tier}"] = route

    # ---- §39 카테고리 특화 모델 alias 추가 ----
    catalog = read_catalog_file(alphred_home)
    cats = catalog.get("categories", {})
    for cat in ("coding", "research", "analysis", "writing", "translation", "creative", "math", "agentic", "general"):
        cat_info = cats.get(cat, {}).get("primary")
        if cat_info and cat_info.get("model"):
            route = {"model": cat_info["model"]}
            if cat_info.get("provider"):
                route["provider"] = cat_info["provider"]
            if cat_info.get("base_url"):
                route["base_url"] = cat_info["base_url"]
            routes[f"alphred-cat-{cat}"] = route
        else:
            if base_model:
                routes[f"alphred-cat-{cat}"] = {k: v for k, v in base_spec.items() if v}

    return routes


def _routes_yaml_block(routes: dict[str, dict], indent: int = 2) -> str:
    """model_routes dict → YAML 블록 텍스트(들여쓰기 indent 기준)."""
    pfx = " " * indent
    pfx2 = " " * (indent * 2)
    lines: list[str] = [f"{pfx}{_ROUTE_KEY}:\n"]
    for alias, spec in routes.items():
        lines.append(f"{pfx2}{alias}:\n")
        for k, v in spec.items():
            lines.append(f"{pfx2}  {k}: {v}\n")
    return "".join(lines)


def write_hermes_model_routes(hermes_home: Path, routes: dict[str, dict]) -> bool:
    """config.yaml 의 model_routes 블록을 멱등 갱신(§38 P1).

    routes 가 비면 기존 블록 제거. 변경이 있으면 True(Hermes 재기동 시그널).
    라인편집으로 주석/순서 보존(코어 무수정 원칙).
    """
    p = model_config_path(hermes_home)
    if not p.exists():
        return False
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return False

    # 기존 model_routes 블록 위치 찾기 — `model_routes:` 로 시작하는 라인부터
    # 다음 같은/낮은 들여쓰기의 키 라인 직전까지.
    lines = text.splitlines(keepends=True)
    start_idx, end_idx = None, None
    route_indent = None
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith(f"{_ROUTE_KEY}:"):
            start_idx = i
            route_indent = len(ln) - len(stripped)
            # 이 라인 이후에서 블록 끝을 찾는다
            for j in range(i + 1, len(lines)):
                s = lines[j].rstrip("\n")
                st = s.lstrip()
                if not st or st.startswith("#"):
                    continue
                indent_j = len(s) - len(st)
                if indent_j <= route_indent:
                    end_idx = j
                    break
            else:
                end_idx = len(lines)
            break

    if not routes:
        # routes 비어있으면 기존 블록 제거
        if start_idx is None:
            return False  # 이미 없음
        new_lines = lines[:start_idx] + lines[end_idx:]
        new_text = "".join(new_lines)
        if new_text == text:
            return False
        p.write_text(new_text, encoding="utf-8")
        return True

    # 새 블록 생성
    new_block = _routes_yaml_block(routes, indent=route_indent or 2)

    if start_idx is not None:
        # 기존 블록 교체
        old_block = "".join(lines[start_idx:end_idx])
        if old_block == new_block:
            return False  # 변경 없음
        new_lines = lines[:start_idx] + [new_block] + lines[end_idx:]
    else:
        # 기존 블록 없음 — 파일 끝에 추가
        new_lines = lines + ["\n", new_block]

    p.write_text("".join(new_lines), encoding="utf-8")
    return True


def read_hermes_model_routes(hermes_home: Path) -> dict[str, dict]:
    """config.yaml 에서 현재 model_routes 를 읽는다(§38 P1 — 검증/테스트용)."""
    p = model_config_path(hermes_home)
    if not p.exists():
        return {}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    in_routes = False
    route_indent = 0
    cur_alias: str | None = None
    cur_spec: dict = {}
    routes: dict[str, dict] = {}
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(f"{_ROUTE_KEY}:"):
            in_routes = True
            route_indent = len(ln.rstrip("\n")) - len(stripped)
            continue
        if not in_routes:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(ln.rstrip("\n")) - len(stripped)
        if indent <= route_indent:
            # 블록 끝
            if cur_alias:
                routes[cur_alias] = cur_spec
            break
        # alias 라인(route_indent+2) 또는 spec 라인(route_indent+4)
        m = re.match(r"^(\w[\w\-]*):\s*(.*)$", stripped)
        if m:
            if indent == route_indent + 2:
                # 새 alias
                if cur_alias:
                    routes[cur_alias] = cur_spec
                cur_alias = m.group(1)
                cur_spec = {}
                # 인라인 값이 있으면(scalr alias 형태)
                val = m.group(2).strip()
                if val:
                    cur_spec["model"] = val.strip("\"'")
            elif indent >= route_indent + 4 and cur_alias:
                cur_spec[m.group(1)] = m.group(2).strip().strip("\"'")
    else:
        if cur_alias:
            routes[cur_alias] = cur_spec
    return routes


def sync_model_routes(alphred_home: Path, hermes_home: Path) -> dict[str, dict]:
    """기동 시 model_routes 를 빌드 → config.yaml 동기화(§38 P1). 현재 routes 반환.

    tier 설정이 없으면 빈 dict(기존 블록도 제거 — 독립 Hermes 사용에 잔류 방지).
    실패는 빈 dict(fail-open — 레거시 모드로 폴백).
    """
    try:
        routes = build_model_routes(alphred_home, hermes_home)
        write_hermes_model_routes(hermes_home, routes)
        return routes
    except Exception:
        return {}


# ---- depth별 모델 라우팅 (§29.1) ----
# Alphred 가 작업 깊이(High/Mid/Light)별로 다른 모델을 쓰도록 사용자 설정을 보관/해석한다.
# 영속 파일 ALPHRED_HOME/models.json:
#   {"high":<spec>, "mid":<spec>, "low":<spec>, "base":<name>, "base_reasoning"?:<level|''>}
#   spec = 모델 이름 문자열 | {"model"?:..., "provider"?:..., "base_url"?:..., "reasoning"?:...}
#          (크로스 프로바이더 + depth별 추론 깊이; model 없이 reasoning 만도 가능)
#   base = tier 미설정 depth 에서 복원할 사용자 기본 모델(첫 tier 설정 시 1회 스냅샷).
#   base_reasoning = tier 미설정 depth 에서 복원할 추론 깊이(첫 reasoning tier 설정 시 스냅샷).
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
    if isinstance(spec, dict):
        out = {k: v for k, v in spec.items()
               if k in ("model", "provider", "base_url", "reasoning") and v}
        if out.get("model") or out.get("reasoning"):
            return out
    return None


# ---- Hermes 추론 깊이(agent.reasoning_effort) 읽기/쓰기 ----
# Hermes hermes_constants.parse_reasoning_effort 의 유효 레벨과 동일해야 한다.
# ''(빈 값) = Hermes 기본(medium). "none" 은 사고 비활성(유효 레벨).
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")
_REASONING_KEYS = ["agent", "reasoning_effort"]


def read_reasoning_effort(hermes_home: Path) -> str | None:
    """config.yaml 의 agent.reasoning_effort 현재값(''=Hermes 기본). 키 부재 시 None."""
    return read_config_scalar(hermes_home, _REASONING_KEYS)


def set_reasoning_effort(hermes_home: Path, value: str) -> bool:
    """agent.reasoning_effort 를 라인편집으로 설정. 멱등(동일값 미기록), 코어 무수정.

    value='' 는 Hermes 기본(medium) 복원 — YAML 빈 문자열('')로 기록한다.
    키가 없으면 agent: 블록 첫 자식으로 삽입한다(구버전 config 대비).
    """
    value = (value or "").strip().lower()
    yaml_val = value if value else "''"
    cur = read_config_scalar(hermes_home, _REASONING_KEYS)
    if cur is not None:
        if cur == value:
            return False
        return set_config_scalar(hermes_home, _REASONING_KEYS, yaml_val)
    try:
        lines = _config_lines(hermes_home)
    except Exception:
        return False
    for i, ln in enumerate(lines):
        if ln.startswith("agent:"):
            indent = 2                          # 자식 들여쓰기 폭은 기존 첫 자식을 따른다
            for nx in lines[i + 1:]:
                s = nx.rstrip("\n")
                st = s.lstrip(" ")
                if st and not st.startswith("#"):
                    if s[:1] in (" ", "\t"):
                        indent = len(s) - len(st)
                    break
            lines.insert(i + 1, " " * indent + f"reasoning_effort: {yaml_val}\n")
            try:
                model_config_path(hermes_home).write_text("".join(lines), encoding="utf-8")
                return True
            except Exception:
                return False
    return False


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
    # §29.1 확장 — depth별 추론 깊이(Hermes agent.reasoning_effort) 오버라이드. 기본 미설정.
    reasoning_high: str | None = None     # ALPHRED_REASONING_HIGH
    reasoning_mid: str | None = None      # ALPHRED_REASONING_MID
    reasoning_low: str | None = None      # ALPHRED_REASONING_LOW
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
    ledger: bool = True                   # §40 세션 작업 원장 — 분류/계획/실행에 주입(무LLM, 기본 on)
    rewrite: bool = False                 # §40 지시어 해소 재작성(LLM 1콜, 참조 감지 시만 — smart+ 기본 ON)
    slots: str = "1"                      # §38 ALPHRED_SLOTS = 정수 또는 "auto" (기본 1)
    slots_max: int = 4                    # §38 ALPHRED_SLOTS_MAX (기본 4)

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
        p_slots, p_slots_max = None, None
        slots_json_path = alphred_home / "slots.json"
        if slots_json_path.exists():
            try:
                import json as _json
                slots_data = _json.loads(slots_json_path.read_text(encoding="utf-8"))
                p_slots = slots_data.get("slots")
                p_slots_max = slots_data.get("slots_max")
            except Exception:
                pass

        env_slots = os.environ.get("ALPHRED_SLOTS")
        slots_val = (env_slots.strip() if env_slots else None) or p_slots or "1"

        env_slots_max = os.environ.get("ALPHRED_SLOTS_MAX")
        slots_max_val = int(env_slots_max) if env_slots_max else (p_slots_max if p_slots_max is not None else 4)

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
            reasoning_high=os.environ.get("ALPHRED_REASONING_HIGH", "").strip().lower() or None,
            reasoning_mid=os.environ.get("ALPHRED_REASONING_MID", "").strip().lower() or None,
            reasoning_low=os.environ.get("ALPHRED_REASONING_LOW", "").strip().lower() or None,
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
            ledger=os.environ.get("ALPHRED_LEDGER", "1").lower() not in ("0", "false", "no"),
            rewrite=_flag("ALPHRED_REWRITE", smart),       # §40 smart+ 기본 ON
            slots=slots_val,
            slots_max=slots_max_val,
        )

    # ---- §29.1 depth별 모델 tier 해석/설정 ----
    @property
    def models_json_path(self) -> Path:
        return self.alphred_home / MODELS_FILENAME

    def _env_tier(self, tier: str) -> str | None:
        return {"high": self.model_high, "mid": self.model_mid,
                "low": self.model_low}.get(tier)

    def has_model_tiers(self) -> bool:
        """사용자가 depth별 **모델**을 하나라도 설정했는가(env 또는 models.json).

        reasoning 만 있는 tier 는 모델 라우팅을 켜지 않는다(has_reasoning_tiers 가 담당).
        """
        if any(self._env_tier(t) for t in _TIERS):
            return True
        data = read_models_file(self.alphred_home)
        for t in _TIERS:
            spec = _norm_spec(data.get(t))
            if spec and spec.get("model"):
                return True
        return False

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

    def model_base_spec(self) -> dict | None:
        """복원용 base spec {model, provider?, base_url?}.

        provider/base_url 은 스냅샷이 있을 때만 포함(구 models.json 은 model 만 —
        그 경우 provider 복원은 불가하므로 기존 동작 유지). ''(빈 값)도 그대로 보존해
        크로스 프로바이더 tier 가 남긴 값을 덮어쓸 수 있게 한다.
        """
        data = read_models_file(self.alphred_home)
        model = data.get("base") or read_default_model(self.hermes_home)
        if not model:
            return None
        spec: dict = {"model": model}
        if "base_provider" in data:
            spec["provider"] = str(data.get("base_provider") or "")
            spec["base_url"] = str(data.get("base_base_url") or "")
        return spec

    # ---- §29.1 확장: depth별 추론 깊이(agent.reasoning_effort) ----
    def _env_reasoning(self, tier: str) -> str | None:
        return {"high": self.reasoning_high, "mid": self.reasoning_mid,
                "low": self.reasoning_low}.get(tier)

    def has_reasoning_tiers(self) -> bool:
        """사용자가 depth별 추론 깊이를 하나라도 설정했는가(env 또는 models.json)."""
        if any(self._env_reasoning(t) for t in _TIERS):
            return True
        data = read_models_file(self.alphred_home)
        return any(isinstance(data.get(t), dict) and data[t].get("reasoning")
                   for t in _TIERS)

    def reasoning_for_depth(self, depth: str | None) -> str | None:
        """작업 심화도 → 명시 설정된 추론 깊이 | None(= base 복원은 적용기가 결정).

        우선순위: 환경변수 > models.json tier(모델 라우팅과 동일).
        """
        tier = _tier_for_depth(depth)
        env_val = self._env_reasoning(tier)
        if env_val:
            return env_val
        spec = read_models_file(self.alphred_home).get(tier)
        if isinstance(spec, dict) and spec.get("reasoning"):
            return str(spec["reasoning"])
        return None

    def reasoning_base_default(self) -> str:
        """tier 미설정 depth 에서 복원할 추론 깊이(스냅샷 → 현재 config, ''=Hermes 기본)."""
        data = read_models_file(self.alphred_home)
        if "base_reasoning" in data:
            return str(data.get("base_reasoning") or "")
        return read_reasoning_effort(self.hermes_home) or ""

    def get_tiers(self) -> dict:
        """현재 depth별 모델·추론 매핑(표시/ API용) — env 우선, 출처 라벨 포함."""
        data = read_models_file(self.alphred_home)
        out: dict = {"base": data.get("base") or read_default_model(self.hermes_home),
                     "base_reasoning": self.reasoning_base_default()}
        for t in _TIERS:
            env_name = self._env_tier(t)
            if env_name:
                out[t] = {"model": env_name, "source": "env"}
            else:
                spec = _norm_spec(data.get(t))
                out[t] = ({**spec, "source": "models.json"} if spec else None)
            env_r = self._env_reasoning(t)          # 추론 깊이는 env 가 tier 값을 덮는다
            if env_r:
                out[t] = {**(out[t] or {"source": "env"}), "reasoning": env_r}
        return out

    def set_tier_model(self, tier: str, spec: dict | str | None) -> None:
        """models.json 에 depth tier 모델·추론 깊이를 영속 설정(spec=None → 해제).

        첫 tier 설정 시 현재 config.default 를 base 로, 첫 reasoning 설정 시 현재
        agent.reasoning_effort 를 base_reasoning 으로 1회 스냅샷한다(미설정 depth 복원용).
        """
        if tier not in _TIERS:
            raise ValueError(f"unknown tier: {tier!r} (high|mid|low)")
        data = read_models_file(self.alphred_home)
        if "base" not in data:
            # 모델명만이 아니라 provider/base_url 도 스냅샷 — 크로스 프로바이더 tier 후
            # 미설정 depth 로 복귀할 때 provider 가 tier 값으로 잔류하지 않도록(복원용).
            mc = read_model_config(self.hermes_home)
            if mc.get("default"):
                data["base"] = mc["default"]
                data["base_provider"] = mc.get("provider") or ""
                data["base_base_url"] = mc.get("base_url") or ""
        if spec is None:
            data.pop(tier, None)
        else:
            norm = _norm_spec(spec)
            if norm is None:
                raise ValueError("invalid model spec")
            r = norm.get("reasoning")
            if r is not None:
                r = str(r).strip().lower()
                if r not in REASONING_LEVELS:
                    raise ValueError(
                        f"reasoning 은 {'|'.join(REASONING_LEVELS)} 중 하나여야 합니다")
                norm["reasoning"] = r
                if "base_reasoning" not in data:
                    data["base_reasoning"] = read_reasoning_effort(self.hermes_home) or ""
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
        had_reasoning = any(isinstance(data.get(t), dict) and data[t].get("reasoning")
                            for t in _TIERS)
        for t in _TIERS:
            data.pop(t, None)
        data.pop("light", None)   # 구 키(light→low 리네임 잔재) 정리
        self._write_models_file(data)
        if had_reasoning and "base_reasoning" in data:
            # tier 해제로 라우팅이 멈추므로, 마지막 tier 값이 config 에 남지 않게 base 복원
            set_reasoning_effort(self.hermes_home, str(data.get("base_reasoning") or ""))

    def resolve_provider_and_model(self, depth: str | None, category: str | None = None) -> tuple[str, str]:
        """주어진 depth 와 category 에 대해 (provider, model) 을 결정적으로 반환한다.

        기본값: provider="hermes" (또는 base_url 로부터 파싱), model은 base_model.
        """
        spec = self.model_for_depth(depth)
        if not spec or not spec.get("model"):
            spec = self.model_base_spec()

        model = (spec or {}).get("model") or read_default_model(self.hermes_home) or "hermes-agent"
        provider = (spec or {}).get("provider") or ""

        if model == "auto":
            catalog = read_catalog_file(self.alphred_home)
            cat = category or "general"
            cat_info = catalog.get("categories", {}).get(cat, {}).get("primary") or {}
            model = cat_info.get("model") or "hermes-agent"
            provider = cat_info.get("provider") or provider

        if not provider:
            # base_url 이나 model 명으로부터 provider 추정
            base_url = (spec or {}).get("base_url") or self.api_base_url
            if "openrouter" in base_url.lower():
                provider = "openrouter"
            elif "nvidia" in base_url.lower() or "nim" in base_url.lower():
                provider = "nvidia"
            else:
                provider = "hermes"

        return provider.lower(), model

    def _venv_python(self) -> Path | None:
        """Hermes venv 의 파이썬 실행 파일."""
        cands = []
        if self.hermes_bin:
            b = Path(self.hermes_bin)
            cands += [b.with_name("python.exe"), b.with_name("python")]
        venv = Path(self.hermes_home) / "hermes-agent" / "venv"
        cands += [venv / "Scripts" / "python.exe", venv / "bin" / "python"]
        for c in cands:
            if c.exists():
                return c
        return None

    def _reasoning_model_ids(self, provider: str | None) -> set[str]:
        """models.dev 카탈로그에서 해당 provider 의 reasoning=True 모델 id 집합."""
        if not provider:
            return set()
        p = self.hermes_home / "models_dev_cache.json"
        if not p.exists():
            return set()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            pl = provider.lower().strip()
            pkey = next((k for k in data if k.lower() == pl
                         and isinstance(data.get(k), dict)
                         and isinstance(data[k].get("models"), dict)), None)
            models = (data.get(pkey) or {}).get("models") if pkey else None
            if isinstance(models, dict):
                return {mid for mid, me in models.items()
                        if isinstance(me, dict) and me.get("reasoning") is True}
        except Exception:
            pass
        return set()

    def fetch_available_models(self) -> dict:
        """선택 가능한 실제 모델 목록 — 모든 인증된 provider + 현재 기본 provider 의 curated 모델들을 조회 및 머지."""
        import subprocess
        model_cfg = read_model_config(self.hermes_home)
        cur = model_cfg.get("default") or ""
        default_provider = model_cfg.get("provider") or (cur.split("/")[0] if "/" in cur else "")
        
        pyexe = self._venv_python()
        if not pyexe:
            return {"current": cur, "current_provider": default_provider, "models": []}
            
        code = (
            "import json\n"
            "from hermes_cli.models import list_available_providers, curated_models_for_provider, normalize_provider, provider_label\n"
            "def_p = %r\n"
            "auth_pids = [p['id'] for p in list_available_providers() if p.get('authenticated')]\n"
            "targets = list(auth_pids)\n"
            "norm_def = normalize_provider(def_p) if def_p else None\n"
            "if norm_def and norm_def not in targets:\n"
            "    targets.append(norm_def)\n"
            "res = {}\n"
            "for p in targets:\n"
            "    norm_p = normalize_provider(p)\n"
            "    if norm_p:\n"
            "        try:\n"
            "            res[norm_p] = {\n"
            "                'label': provider_label(norm_p),\n"
            "                'models': [m for m, _ in curated_models_for_provider(norm_p)]\n"
            "            }\n"
            "        except Exception:\n"
            "            pass\n"
            "print(json.dumps(res))\n" % default_provider
        )
        
        try:
            env = {**os.environ, "PYTHONUTF8": "1"}
            env_file = self.hermes_home / ".env"
            if env_file.exists():
                try:
                    for line in env_file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env[k.strip()] = v.strip().strip("'\"")
                except Exception:
                    pass
            cflags = 0x08000000 if os.name == "nt" else 0
            out = subprocess.run([str(pyexe), "-c", code], cwd=str(self.hermes_home / "hermes-agent"),
                                 capture_output=True, text=True, timeout=15, env=env, creationflags=cflags)
            if out.returncode == 0 and out.stdout.strip():
                raw_data = json.loads(out.stdout.strip())
            else:
                raw_data = {}
        except Exception:
            raw_data = {}
            
        catalog = read_catalog_file(self.alphred_home)
        categories = catalog.get("categories", {})
        
        merged_models = []
        for provider_id, p_info in raw_data.items():
            label = p_info.get("label") or provider_id
            models_list = p_info.get("models") or []
            rset = self._reasoning_model_ids(provider_id)
            
            for m in models_list:
                matched_cats = []
                for cat_name, cat_data in categories.items():
                    primary = cat_data.get("primary") or {}
                    p_model = primary.get("model")
                    if not p_model:
                        continue
                    p_clean = p_model.split(":")[0].strip()
                    m_clean = m.split(":")[0].strip()
                    p_base = p_clean.split("/")[-1] if "/" in p_clean else p_clean
                    m_base = m_clean.split("/")[-1] if "/" in m_clean else m_clean
                    is_match = (p_clean.lower() == m_clean.lower()) or (p_base.lower() == m_base.lower())
                    if is_match:
                        matched_cats.append(cat_name)
                            
                merged_models.append({
                    "id": m,
                    "provider": provider_id,
                    "provider_label": label,
                    "categories": matched_cats,
                    "reasoning": m in rset
                })
                
        merged_models.sort(key=lambda x: (x["provider_label"], x["id"]))
        
        cur_provider = default_provider
        cur_rset = self._reasoning_model_ids(cur_provider)
        current_reasoning = bool(cur and cur in cur_rset)
        
        return {
            "current": cur,
            "current_provider": cur_provider,
            "current_reasoning": current_reasoning,
            "models": merged_models
        }


