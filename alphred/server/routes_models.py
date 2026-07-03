"""모델 라우트 — 선택 가능 모델 목록(§29.1)·depth별 tier 설정."""
from __future__ import annotations

import json
import os
import subprocess
from fastapi import APIRouter, Depends, HTTPException

from ..config import read_model_config
from .deps import GatewayDeps, make_auth


_reasoning_cache: dict = {"key": None, "ids": set()}


def _reasoning_model_ids(hermes_home, provider: str | None) -> set[str]:
    """models.dev 카탈로그에서 **해당 provider** 의 reasoning=True 모델 id 집합(정확 매칭, §33).

    카탈로그는 100+ 리셀러를 담고 있어 bare-이름 전역 스캔은 오염된다(어떤 리셀러가 같은 모델을
    reasoning=True 로 표시). 실제 provider(예: `nvidia`=NIM) 엔트리로 한정하고 provider 가 쓰는
    **동일한 full id**(예: `google/gemma-4-31b-it`)로 정확 매칭한다. mtime+provider 캐시.
    """
    if not provider:
        return set()
    from pathlib import Path
    p = Path(hermes_home) / "models_dev_cache.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return set()
    ckey = (mt, provider.lower())
    if _reasoning_cache["key"] == ckey:
        return _reasoning_cache["ids"]
    ids: set[str] = set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        pl = provider.lower().strip()
        pkey = next((k for k in data if k.lower() == pl
                     and isinstance(data.get(k), dict)
                     and isinstance(data[k].get("models"), dict)), None)
        models = (data.get(pkey) or {}).get("models") if pkey else None
        if isinstance(models, dict):
            ids = {mid for mid, me in models.items()
                   if isinstance(me, dict) and me.get("reasoning") is True}
    except Exception:
        ids = set()
    _reasoning_cache["key"] = ckey
    _reasoning_cache["ids"] = ids
    return ids


def _is_reasoning(name: str, rset: set[str]) -> bool:
    return bool(name) and name in rset


def _curated_models(cfg, provider: str | None) -> dict:
    """Hermes venv 의 hermes_cli.models 로 provider별 큐레이션 모델 목록을 조회(shell-out)."""
    from pathlib import Path
    if not provider or not cfg.hermes_bin:
        return {}
    pyexe = Path(cfg.hermes_bin).with_name("python.exe")
    if not pyexe.exists():
        return {}
    code = (
        "import json;from hermes_cli.models import curated_models_for_provider,"
        "normalize_provider,provider_label;p=normalize_provider(%r);"
        "print(json.dumps({'label':provider_label(p),"
        "'models':[m for m,_ in curated_models_for_provider(p)]}))" % provider
    )
    try:
        env = {**os.environ, "PYTHONUTF8": "1"}
        out = subprocess.run([str(pyexe), "-c", code], cwd=str(cfg.hermes_home / "hermes-agent"),
                             capture_output=True, text=True, timeout=15, env=env)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip())
    except Exception:
        pass
    return {}


def build_router(deps: GatewayDeps) -> APIRouter:
    router = APIRouter(dependencies=[Depends(make_auth(deps.cfg))])
    cfg = deps.cfg

    @router.get("/models/available")
    def models_available():
        """선택 가능한 실제 모델 목록 — Hermes 의 curated 모델 레지스트리(provider별)를 조회.

        :8642 의 /v1/models 는 메타모델(hermes-agent)만 주므로, config 의 현재 모델에서
        provider 를 추론해 Hermes venv 의 hermes_cli.models 로 큐레이션 목록을 가져온다.
        """
        model_cfg = read_model_config(cfg.hermes_home)
        cur = model_cfg.get("default")
        # 실제 provider 는 model.provider 가 우선(예: default=google/gemma-... 인데 provider=nvidia).
        provider = model_cfg.get("provider") or (
            cur.split("/")[0] if cur and "/" in cur else None)
        info = _curated_models(cfg, provider)
        models = info.get("models", [])
        rset = _reasoning_model_ids(cfg.hermes_home, provider)   # §33 provider 스코프 정확매칭
        reasoning = [m for m in models if _is_reasoning(m, rset)]
        return {"current": cur, "provider": info.get("label") or provider,
                "models": models, "reasoning": reasoning,
                "current_reasoning": _is_reasoning(cur or "", rset)}

    @router.get("/models/tiers")
    def models_tiers():
        """depth별 모델 매핑 조회(§29.1) — high/mid/low → 모델 + base."""
        return {"tiers": cfg.get_tiers(), "enabled": cfg.has_model_tiers()}

    @router.post("/models/tiers")
    def set_models_tier(body: dict):
        """depth tier 모델 설정/해제(§29.1). body={tier:high|mid|low, model:<name>|null,
        provider?, base_url?}. model=null/"" → 해제(base 사용)."""
        tier = str(body.get("tier") or "").strip().lower()
        if tier not in ("high", "mid", "low"):
            raise HTTPException(status_code=400, detail="tier must be high|mid|low")
        name = body.get("model")
        spec = None
        if name:
            spec = {"model": str(name)}
            if body.get("provider"):
                spec["provider"] = str(body["provider"])
            if body.get("base_url"):
                spec["base_url"] = str(body["base_url"])
        try:
            cfg.set_tier_model(tier, spec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"tiers": cfg.get_tiers(), "enabled": cfg.has_model_tiers()}

    @router.post("/models/default")
    def set_default_model(body: dict):
        """모델을 **영구 기본값**으로 설정(config.yaml default + models.json base, 깊이별 tier 해제).

        사용자가 다시 바꾸기 전까지 유지되고 §29.1 라우팅이 덮어쓰지 않는다.
        `known` = 이 모델명이 provider 큐레이션 목록에 있는지(오타/접두어 검증, 비차단).
        """
        name = str(body.get("model") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="model required")
        try:
            cfg.set_default_model(name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        model_cfg = read_model_config(cfg.hermes_home)
        provider = model_cfg.get("provider") or (name.split("/")[0] if "/" in name else None)
        models = _curated_models(cfg, provider).get("models") or []
        known = (not models) or (name in models)   # 목록 못 가져오면 검증 생략(참으로 처리)
        return {"default": model_cfg.get("default"), "known": known,
                "provider": provider, "models_available": bool(models)}

    return router
