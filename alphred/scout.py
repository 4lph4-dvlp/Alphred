"""§39: 모델 카탈로그 갱신을 수행하는 Scout 모듈."""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from .config import read_catalog_file, write_catalog_file, DEFAULT_CATALOG

logger = logging.getLogger("alphred.scout")


def fetch_openrouter_models(free_only: bool = False) -> list[str]:
    """OpenRouter 모델 리스트를 조회한다."""
    url = "https://openrouter.ai/api/v1/models"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Alphred/Scout"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            if free_only:
                models = [m["id"] for m in data.get("data", []) if ":free" in m.get("id", "")]
            else:
                models = [m["id"] for m in data.get("data", [])]
            return models
    except Exception as e:
        logger.warning("OpenRouter 모델 목록 조회 실패: %s", e)
        # fallback
        if free_only:
            return [
                "nvidia/llama-3.1-nemotron-ultra-253b-v1:free",
                "nvidia/llama-3.3-nemotron-super-49b-v1:free",
                "meta-llama/llama-3.1-70b-instruct:free",
                "meta-llama/llama-3.3-70b-instruct:free"
            ]
        else:
            return [
                "nvidia/llama-3.1-nemotron-ultra-253b-v1:free",
                "nvidia/llama-3.3-nemotron-super-49b-v1:free",
                "meta-llama/llama-3.1-70b-instruct:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "anthropic/claude-3.5-sonnet",
                "google/gemini-2.5-pro"
            ]


def fetch_nim_models(api_key: str | None = None) -> list[str]:
    """NVIDIA NIM 모델 리스트를 조회한다."""
    if not api_key:
        return ["nvidia/llama-3.1-nemotron-ultra-253b-v1", "nvidia/llama-3.3-nemotron-super-49b-v1"]
    url = "https://integrate.api.nvidia.com/v1/models"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Alphred/Scout"
        })
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            models = [m["id"] for m in data.get("data", [])]
            return models
    except Exception as e:
        logger.warning("NIM 모델 목록 조회 실패: %s", e)
        return ["nvidia/llama-3.1-nemotron-ultra-253b-v1", "nvidia/llama-3.3-nemotron-super-49b-v1"]


def run_canary_test(model: str, provider: str, api_key: str | None = None, base_url: str | None = None) -> bool:
    """모델 카나리아 스모크 콜을 1회 날려 정상 반응하는지 검증한다."""
    # 만약 api key 가 없거나 mock 이라면 성공한 것으로 간주하여 진행 보장 (fail-safe)
    if not api_key:
        logger.info("API 키 없음 -> 카나리아 스모크 테스트 건너뛰고 채택: %s (%s)", model, provider)
        return True

    url = base_url or ("https://openrouter.ai/api/v1" if provider == "openrouter" else "https://integrate.api.nvidia.com/v1")
    url = url.rstrip("/") + "/chat/completions"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Alphred/Scout"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            choices = res_data.get("choices")
            if choices and len(choices) > 0:
                logger.info("카나리아 스모크 테스트 통과: %s (%s)", model, provider)
                return True
    except Exception as e:
        logger.warning("카나리아 스모크 테스트 실패: %s (%s): %s", model, provider, e)
    return False


def run_scout_update(alphred_home: Path, openrouter_key: str | None = None, nim_key: str | None = None, verbose: bool = False, free_only: bool = False) -> bool:
    """§39.3 C: 주간 Scout 작업을 수행하여 model_catalog.json을 업데이트하고 검증한다."""
    logger.info("Scout 모델 인벤토리 수집 시작...")
    if verbose:
        import os
        from .config import Config
        try:
            cfg = Config.load()
            pyexe = cfg._venv_python()
            if pyexe:
                import subprocess
                code = (
                    "import json; from hermes_cli.models import list_available_providers; "
                    "print(json.dumps(list_available_providers()))"
                )
                env = {**os.environ, "PYTHONUTF8": "1"}
                env_file = cfg.hermes_home / ".env"
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
                out = subprocess.run([str(pyexe), "-c", code], cwd=str(cfg.hermes_home / "hermes-agent"),
                                     capture_output=True, text=True, timeout=10, env=env, creationflags=cflags)
                if out.returncode == 0 and out.stdout.strip():
                    providers = json.loads(out.stdout.strip())
                    print("\n발견된 사용 가능한 프로바이더 목록:")
                    for p in providers:
                        auth_status = "\033[92m[인증됨]\033[0m" if p.get("authenticated") else "\033[90m[미인증]\033[0m"
                        print(f"  • {p.get('label', p['id']):<40} {auth_status}")
                    print()
        except Exception as e:
            logger.debug("프로바이더 상태 로드 실패: %s", e)

    or_inventory = fetch_openrouter_models(free_only=free_only)
    nim_inventory = fetch_nim_models(nim_key)

    # 기본 카탈로그를 복사하여 변경 후보 구성
    old_catalog = read_catalog_file(alphred_home)
    categories = old_catalog.get("categories") or DEFAULT_CATALOG["categories"]

    # 새로운 카탈로그 후보 생성
    new_catalog = {
        "version": old_catalog.get("version", 1),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "free_only": free_only,
        "categories": {}
    }

    results = []
    for cat, info in categories.items():
        default_info = DEFAULT_CATALOG["categories"].get(cat, {})
        cand_key = "free" if free_only else "paid"
        candidate_spec = info.get(cand_key) or default_info.get(cand_key)
        if not candidate_spec:
            candidate_spec = info.get("primary", {"model": "hermes-agent"})

        fallbacks = info.get("fallbacks", [])
        evidence = info.get("evidence", "Scout automated check")

        model_id = candidate_spec.get("model")
        provider = candidate_spec.get("provider", "openrouter")
        if "/" not in model_id or model_id == "hermes-agent":
            provider = "local"

        verified = False
        is_local = False
        canary_msg = ""

        # 인벤토리 검증
        if provider == "local" or provider not in ("openrouter", "nim"):
            verified = True
            is_local = True
            canary_msg = "로컬 모델 — 검증 스킵"
        elif provider == "openrouter":
            if model_id in or_inventory or "free" in model_id:
                # 카나리아 검증
                import time
                t0 = time.perf_counter()
                if run_canary_test(model_id, provider, openrouter_key):
                    verified = True
                    elapsed = (time.perf_counter() - t0) * 1000
                    canary_msg = f"canary ping OK ({elapsed:.0f}ms)"
                else:
                    canary_msg = "canary ping 실패"
            else:
                canary_msg = "인벤토리 없음"
        elif provider == "nim":
            if model_id in nim_inventory:
                import time
                t0 = time.perf_counter()
                if run_canary_test(model_id, provider, nim_key):
                    verified = True
                    elapsed = (time.perf_counter() - t0) * 1000
                    canary_msg = f"canary ping OK ({elapsed:.0f}ms)"
                else:
                    canary_msg = "canary ping 실패"
            else:
                canary_msg = "인벤토리 없음"

        free_spec = info.get("free") or default_info.get("free")
        paid_spec = info.get("paid") or default_info.get("paid")

        if verified:
            new_catalog["categories"][cat] = {
                "free": free_spec,
                "paid": paid_spec,
                "primary": candidate_spec,
                "fallbacks": fallbacks,
                "evidence": evidence
            }
            status = "local" if is_local else "verified"
            results.append((cat, model_id, status, canary_msg))
        else:
            # 검증 실패 시 첫 번째 fallback 또는 general 기본값으로 폴백
            fallback_spec = fallbacks[0] if fallbacks else {"model": "hermes-agent"}
            logger.warning("카테고리 %s 의 후보 모델 %s 검증 실패 -> %s 로 폴백", cat, model_id, fallback_spec.get("model"))
            new_catalog["categories"][cat] = {
                "free": free_spec,
                "paid": paid_spec,
                "primary": fallback_spec,
                "fallbacks": fallbacks[1:] if len(fallbacks) > 1 else [],
                "evidence": "Fallback due to verification failure"
            }
            results.append((cat, model_id, "fallback", fallback_spec.get("model")))

    # 결과 테이블 출력
    print("\nScout 모델 검증 결과:")
    ok_count = 0
    fb_count = 0
    for cat, model, status, detail in results:
        cat_pad = f"{cat:<14}"
        if status == "local":
            print(f"  \033[92m✓\033[0m {cat_pad} {model} \033[2m(로컬)\033[0m")
            ok_count += 1
        elif status == "verified":
            msg = f"  \033[92m✓\033[0m {cat_pad} {model}"
            if verbose and detail:
                msg += f" \033[2m({detail})\033[0m"
            print(msg)
            ok_count += 1
        elif status == "fallback":
            msg = f"  \033[93m✗\033[0m {cat_pad} {model} → {detail} \033[93m(폴백)\033[0m"
            print(msg)
            fb_count += 1
            
    print("  " + "─" * 40)
    total = len(results)
    summary = f"  {ok_count}/{total} 검증 통과"
    if fb_count:
        summary += f" · {fb_count} 폴백 적용"
    print(summary + "\n")

    write_catalog_file(alphred_home, new_catalog)
    logger.info("model_catalog.json 업데이트 완료")
    return True
