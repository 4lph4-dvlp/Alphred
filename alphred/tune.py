"""§29.3 Hermes 설정 품질 감사·적용 — `alphred tune`.

보고서가 지적한 순정 Hermes 품질 저하 원인 중 **설정으로 완화 가능한 것**(컨텍스트 압축 #1,
보조모델 하향 #2, 툴서치 과부하 #4, 웹검색 한계 #5)을 진단하고, 동의 시에만 사용자 config.yaml
을 **백업 후 멱등 라인편집**으로 교정한다. Hermes 코어/소스는 절대 건드리지 않으며, 모든 편집은
사용자 파일(업데이트 내성)이고 `--revert` 로 원복된다. 라이브 LLM 호출 없음(쿼터 안전).
"""
from __future__ import annotations

import shutil

from .config import Config, model_config_path, read_config_scalar, set_config_scalar

# 권장 설정 knob. rec=None 이면 자문만(자동 적용 안 함 — 외부 키/판단 필요).
KNOBS = [
    {"id": "compress_first", "keys": ["compression", "protect_first_n"], "rec": "6",
     "cause": "#1", "label": "압축 보호(앞 메시지 수)",
     "why": "초기 지시·제약이 컨텍스트 압축에 사라지지 않도록 앞 메시지를 더 보존"},
    {"id": "compress_threshold", "keys": ["compression", "threshold"], "rec": "0.7",
     "cause": "#1", "label": "압축 발동 임계",
     "why": "더 늦게 압축해 미묘한 제약·추론을 오래 보존(0.5→0.7)"},
    {"id": "toolsearch_threshold", "keys": ["tools", "tool_search", "threshold_pct"], "rec": "25",
     "cause": "#4", "label": "툴서치 발동 임계%",
     "why": "툴 스키마를 더 늦게 숨겨 약한 모델의 도구 탐색 실패·환각을 줄임(10→25)"},
    {"id": "websearch", "keys": ["web", "backend"], "rec": None,
     "cause": "#5", "label": "웹검색 백엔드",
     "why": "brave-free → Tavily/SearXNG 권장(인용 보존·리서치 깊이↑). API 키 필요 → 자문만"},
]

# #2: 보조모델 오버라이드 점검 대상(모델이 비어있지 않으면 수동 지정 = 약모델 위험 경고).
_AUX = ["vision", "web_extract", "compression", "skills_hub", "approval", "mcp",
        "title_generation", "triage_specifier", "kanban_decomposer",
        "profile_describer", "curator"]


def _backup_path(cfg: Config):
    return model_config_path(cfg.hermes_home).with_suffix(".yaml.alphred-tune.bak")


def audit(cfg: Config) -> dict:
    """현재 설정을 진단(LLM 호출 없음). 반환: {rows, aux_overrides}."""
    rows = []
    for k in KNOBS:
        cur = read_config_scalar(cfg.hermes_home, k["keys"])
        advisory = k["rec"] is None
        action = (not advisory) and (cur is not None) and (str(cur) != str(k["rec"]))
        rows.append({"id": k["id"], "label": k["label"], "cause": k["cause"],
                     "path": ".".join(k["keys"]), "current": cur, "recommended": k["rec"],
                     "advisory": advisory, "action": action, "why": k["why"]})
    aux_overrides = []
    for a in _AUX:
        m = read_config_scalar(cfg.hermes_home, ["auxiliary", a, "model"])
        if m:                                  # '' 가 아니면 사용자가 보조모델을 수동 지정함
            aux_overrides.append({"slot": a, "model": m})
    return {"rows": rows, "aux_overrides": aux_overrides}


def applicable_ids(cfg: Config) -> list[str]:
    """자동 적용 가능한(자문 아님 + 권장과 다른) knob id 목록."""
    return [r["id"] for r in audit(cfg)["rows"] if r["action"]]


def apply(cfg: Config, ids: list[str] | None = None) -> dict:
    """동의된 knob 을 적용(백업 우선, 멱등). ids=None → 적용 가능한 전부.

    반환: {applied:[id], skipped:[id], backup:path}.
    """
    backup = _backup_path(cfg)
    if not backup.exists():
        shutil.copy2(model_config_path(cfg.hermes_home), backup)
    targets = set(ids) if ids else None
    applied, skipped = [], []
    for k in KNOBS:
        if k["rec"] is None:
            continue
        if targets is not None and k["id"] not in targets:
            continue
        if set_config_scalar(cfg.hermes_home, k["keys"], k["rec"]):
            applied.append(k["id"])
        else:
            skipped.append(k["id"])               # 이미 권장값이거나 키 부재
    return {"applied": applied, "skipped": skipped, "backup": str(backup)}


def revert(cfg: Config) -> bool:
    """tune 백업이 있으면 config.yaml 을 원복한다."""
    backup = _backup_path(cfg)
    if backup.exists():
        shutil.copy2(backup, model_config_path(cfg.hermes_home))
        return True
    return False


# ---- §29.3 확장: 임의 스칼라 get/set — Hermes 상세 설정 전체 표면 커버 ----
# KNOBS(권장 4종) 밖의 설정(agent.max_turns, auxiliary.*.model, tool_output.* 등)도
# Alphred 에서 직접 조회·조정할 수 있게 한다. 편집 규칙은 KNOBS 와 동일:
# 백업 우선·멱등 라인편집·코어 무수정·--revert 로 원복.

def get_scalar(cfg: Config, path: str) -> str | None:
    """config.yaml 임의 스칼라 조회 — path 는 "agent.max_turns" 점 표기."""
    keys = [k for k in (path or "").split(".") if k]
    return read_config_scalar(cfg.hermes_home, keys) if keys else None


def set_scalar(cfg: Config, path: str, value) -> dict:
    """config.yaml 임의 스칼라 설정(백업 우선, 멱등) → {ok, changed, current, error?}.

    키 부재 시 실패 — 신규 키 삽입은 지원하지 않는다(오타가 무의미한 키로 조용히
    저장되는 것을 방지. Hermes 가 기본 config 에 쓰는 키만 대상).
    """
    keys = [k for k in (path or "").split(".") if k]
    if not keys:
        return {"ok": False, "changed": False, "error": "빈 경로"}
    cur = read_config_scalar(cfg.hermes_home, keys)
    if cur is None:
        return {"ok": False, "changed": False,
                "error": f"config.yaml 에 '{path}' 스칼라 키가 없습니다(신규 삽입 미지원)"}
    if str(cur) == str(value):
        return {"ok": True, "changed": False, "current": cur}
    backup = _backup_path(cfg)
    if not backup.exists():
        shutil.copy2(model_config_path(cfg.hermes_home), backup)
    ok = set_config_scalar(cfg.hermes_home, keys, value)
    return {"ok": ok, "changed": ok, "current": str(value) if ok else cur,
            "backup": str(backup)}
