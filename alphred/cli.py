"""alphred CLI — Hermes 의 1:1 drop-in 슈퍼셋.

설계(QA-1):
  - `alphred queue ...` 만 가로챈다(Alphred 신규).
  - 그 외 모든 서브커맨드는 `hermes` 로 **동적 위임**(verbatim). 하드코딩 목록이 아니므로
    신규 Hermes 명령도 자동 노출된다(QA-1.3/1.7). 종료코드·stdin/out/err 패스스루(QA-1.4).
"""
from __future__ import annotations

import os
import subprocess
import sys

from .config import Config
from .runtime import build_manager, resolve_task_id

# Alphred 가 직접 처리하는 서브커맨드(나머지는 전부 Hermes 로 위임)
# 주의: `gateway` 는 가로채지 않고 Hermes 로 위임해 1:1 매핑 유지(QA-1.3).
#       Alphred 자체 HTTP 게이트웨이는 `alphred serve` 로 띄운다.
_INTERCEPT = {"queue", "serve", "setup", "tui", "doctor"}


def _delegate_to_hermes(argv: list[str]) -> int:
    cfg = Config.load()
    if not cfg.hermes_bin:
        sys.stderr.write(
            "alphred: hermes 실행 파일을 찾을 수 없습니다. "
            "PATH 에 추가하거나 ALPHRED_HERMES_BIN 을 설정하세요.\n"
        )
        return 127
    # stdio 를 그대로 상속 → 색상/스트리밍/대화형 입력 보존.
    # PYTHONUTF8=1: 한국어 Windows(cp949) 로캘에서 Hermes 의 유니코드 입출력 안정화.
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run([cfg.hermes_bin, *argv], env=env)
    return proc.returncode


def _cmd_queue(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="alphred queue", description="Alphred 우선순위 큐 관리")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="큐 조회")
    sp = sub.add_parser("submit", help="작업 제출")
    sp.add_argument("prompt")
    sp.add_argument("--priority", type=int, default=None)
    sp.add_argument("--kind", choices=["light", "heavy"], default=None)
    sp.add_argument("--source", default="api")
    g = sub.add_parser("show", help="작업 상세"); g.add_argument("id")
    pr = sub.add_parser("prio", help="우선순위 변경"); pr.add_argument("id"); pr.add_argument("priority", type=int)
    dc = sub.add_parser("discard", help="작업 폐기"); dc.add_argument("id")
    pa = sub.add_parser("pause", help="진행 중 작업 일시중지(사용자)"); pa.add_argument("id")
    re_ = sub.add_parser("resume", help="일시중지 작업 재개 허용"); re_.add_argument("id")
    rt = sub.add_parser("retry", help="검토 필요 작업을 다시 대기열에 올림"); rt.add_argument("id")
    aq = sub.add_parser("ask", help="자연어로 큐 조회/우선순위 변경/삭제")
    aq.add_argument("request")
    sub.add_parser("tick", help="스케줄러 1회 실행")
    rn = sub.add_parser("run", help="스케줄러 루프 실행"); rn.add_argument("--interval", type=float, default=2.0)
    sub.add_parser("sync", help="QUEUE.MD 재생성")
    sf = sub.add_parser("safety", help="안전망 상태/리셋(#30719)"); sf.add_argument("--reset", action="store_true")
    sub.add_parser("cron-tick", help="만료된 cron 작업을 큐로 편입(1회)")

    args = p.parse_args(argv)
    cfg = Config.load()
    mgr, store, client = build_manager(cfg)
    try:
        if args.action == "list":
            _print_list(mgr.list())
        elif args.action == "submit":
            from .safety import BlockedPayloadError
            try:
                t = mgr.submit(args.prompt, source=args.source, priority=args.priority, kind=args.kind)
            except BlockedPayloadError as e:
                print(f"차단됨(안전망): {e.reason}")
                return 3
            print(f"제출됨: {t.id}  kind={t.kind} prio={t.priority} state={t.state}")
            print(f"사유: {t.classify_reason}")
        elif args.action == "safety":
            from .safety import RestartGuard
            guard = RestartGuard(cfg.guard_path, cfg.restart_window_seconds, cfg.restart_threshold)
            if args.reset:
                guard.reset()
                print("안전망 리셋됨 (재시작 기록 삭제)")
            print(f"재시작 기록: {guard.count()}회 / {cfg.restart_window_seconds:.0f}초 "
                  f"(임계 {cfg.restart_threshold}) → tripped={guard.tripped()}")
        elif args.action == "show":
            t = mgr.get(resolve_task_id(store, args.id))
            if not t:
                print("없음"); return 1
            _print_task(t, store)
        elif args.action == "prio":
            t = mgr.reprioritize(resolve_task_id(store, args.id), args.priority)
            print(f"변경됨: {t.id[:8]} → priority={t.priority}")
        elif args.action == "discard":
            t = mgr.discard(resolve_task_id(store, args.id))
            print(f"폐기됨: {t.id[:8]} → {t.state}")
        elif args.action == "pause":
            t = mgr.pause(resolve_task_id(store, args.id))
            print(f"일시중지됨: {t.id[:8]} → {t.state}")
        elif args.action == "resume":
            t = mgr.resume(resolve_task_id(store, args.id))
            print(f"재개 허용됨: {t.id[:8]} (다음 스케줄에서 재개)")
        elif args.action == "retry":
            t = mgr.requeue(resolve_task_id(store, args.id))
            print(f"재시도 대기열 등록: {t.id[:8]} → {t.state}")
        elif args.action == "ask":
            from . import nlq
            out = nlq.ask(mgr, store, args.request, nlq.make_hermes_llm(client))
            if out["reply"]:
                print(out["reply"])
            for r in out["results"]:
                print(f"  • {r}")
        elif args.action == "tick":
            mgr.tick(); print("tick 완료"); _print_list(mgr.list())
        elif args.action == "run":
            _run_loop(mgr, args.interval)
        elif args.action == "sync":
            mgr.sync_md(); print(f"QUEUE.MD 갱신: {cfg.queue_md_path}")
        elif args.action == "cron-tick":
            from .cron_intercept import CronIntercept
            cron = CronIntercept(mgr, cfg.cron_jobs_path, cfg.cron_state_path)
            ids = cron.tick()
            print(f"cron 편입: {len(ids)}건  (jobs={cfg.cron_jobs_path})")
            for i in ids:
                print(f"  → {i[:8]}")
        return 0
    finally:
        client.close()
        store.close()


def _print_list(tasks) -> None:
    if not tasks:
        print("(큐가 비어 있음)"); return
    print(f"{'ID':10} {'PRIO':>4} {'STATE':12} {'KIND':6} PROMPT")
    for t in tasks:
        prompt = (t.prompt or "").replace("\n", " ")[:50]
        print(f"{t.id[:8]:10} {t.priority:>4} {t.state:12} {t.kind:6} {prompt}")


def _print_task(t, store) -> None:
    print(f"ID:        {t.id}")
    print(f"상태:      {t.state}   우선순위: {t.priority}   kind: {t.kind}")
    print(f"소스:      {t.source}   분류근거: {t.classify_reason}")
    print(f"run_id:    {t.hermes_run_id}")
    print(f"생성:      {t.created_at}")
    print(f"prompt:    {t.prompt}")
    if t.result:
        print(f"결과:\n{t.result}")
    if t.error:
        print(f"에러:      {t.error}")
    print("이벤트:")
    for e in store.events(t.id):
        print(f"  {e['at']}  {e['from_state']} → {e['to_state']}  ({e['reason']})")


def _run_loop(mgr, interval: float) -> None:
    import time
    print(f"스케줄러 루프 시작 (interval={interval}s, Ctrl+C 종료)")
    try:
        while True:
            mgr.tick()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n스케줄러 종료")


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 콘솔(cp949)에서도 한글/유니코드 출력 보존
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _INTERCEPT:
        if argv[0] == "queue":
            return _cmd_queue(argv[1:])
        if argv[0] == "serve":
            return _cmd_serve(argv[1:])
        if argv[0] == "setup":
            return _cmd_setup(argv[1:])
        if argv[0] == "tui":
            return _cmd_tui(argv[1:])
        if argv[0] == "doctor":
            return _cmd_doctor(argv[1:])
    # 무인자 `alphred` = 전용 Alphred TUI(기획 §13). Hermes 직접 진입은 `alphred chat`.
    if not argv or argv == ["--no-daemon"]:
        return _cmd_tui(["--no-daemon"] if argv == ["--no-daemon"] else [])
    # 그 외 전부 Hermes 로 위임
    return _delegate_to_hermes(argv)


def _ensure_daemon() -> None:
    """:8643 Alphred 서비스가 안 떠 있으면 백그라운드로 자동 기동(best-effort)."""
    try:
        import httpx
        cfg = Config.load()
        try:
            if httpx.get(f"{cfg.gateway_url}/", timeout=2.0).status_code < 500:
                return  # 이미 가동 중
        except Exception:
            pass
        import subprocess
        log = open(cfg.alphred_home / "serve.log", "ab")
        kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP → TUI 와 독립
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "alphred.cli", "serve"], **kwargs)
        sys.stderr.write("alphred: 백그라운드 큐 서비스를 시작하는 중...\n")
        # :8643 준비를 잠시 대기 → 첫 메시지부터 라우팅 훅이 살아있도록(기획 Fix B).
        # serve 는 Hermes 준비를 블로킹하지 않으므로 보통 1~2초면 뜬다.
        import time
        ready = False
        for _ in range(30):  # 최대 ~15초
            try:
                if httpx.get(f"{cfg.gateway_url}/", timeout=1.0).status_code < 500:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if ready:
            sys.stderr.write("alphred: 큐 서비스 준비 완료.\n")
        else:
            sys.stderr.write("alphred: 큐 준비가 지연됩니다 (채팅은 정상; 큐는 곧 활성화). "
                             "끄려면 ALPHRED_NO_DAEMON=1 또는 `alphred --no-daemon`.\n")
    except Exception:
        pass  # 데몬 기동 실패가 TUI 진입을 막지 않도록


def _cmd_setup(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="alphred setup",
        description="Alphred 초기 설정 — Hermes(LLM provider 등) 온보딩. Hermes 는 순정 유지(브랜딩 안 함).")
    p.add_argument("--no-launch", action="store_true",
                   help="Hermes 온보딩에 진입하지 않고 안내만")
    args = p.parse_args(argv)

    cfg = Config.load()
    if not cfg.hermes_bin:
        sys.stderr.write(
            "alphred: hermes 실행 파일을 찾을 수 없습니다. "
            "Hermes 를 먼저 설치하거나 ALPHRED_HERMES_BIN 을 설정하세요.\n")
        return 127

    # 새 컨셉(§15): Alphred 는 Hermes 를 브랜딩하지 않는다(순정 유지). 정체성은 전용 TUI 가 담당.
    print("Alphred 설정: Hermes 는 순정 그대로 둡니다(스킨/정체성/훅 미설치).")
    if args.no_launch:
        print("준비 완료. 큐 결합 대화는 `alphred`, 순수 Hermes 는 `hermes`(또는 `alphred chat`).")
        return 0
    print("Hermes 온보딩으로 진입합니다… (LLM provider 등 설정)")
    return _delegate_to_hermes([])


def _cmd_doctor(argv: list[str]) -> int:
    """진단(§12.4 D2) — :8642/:8643/모델/큐/플래너 상태를 한 번에 점검·출력."""
    import argparse

    p = argparse.ArgumentParser(prog="alphred doctor",
                                description="Alphred/Hermes 런타임 상태 일괄 점검(관측성)")
    p.add_argument("--json", action="store_true", help="JSON 으로 출력")
    args = p.parse_args(argv)
    cfg = Config.load()
    report = _collect_doctor(cfg)
    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    _print_doctor(report)
    return 0 if report["ok"] else 1


def _collect_doctor(cfg: Config) -> dict:
    """런타임 상태를 수집한다(라이브 LLM 호출 없음 — 쿼터 보호)."""
    import httpx

    def _ping(url: str) -> dict:
        try:
            r = httpx.get(url, timeout=2.5)
            return {"up": r.status_code < 500, "status": r.status_code}
        except Exception as e:
            return {"up": False, "error": type(e).__name__}

    rep: dict = {"checks": [], "ok": True}

    def add(name: str, ok: bool, detail: str) -> None:
        rep["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            rep["ok"] = False

    # 1) hermes 바이너리
    add("hermes 실행파일", bool(cfg.hermes_bin), cfg.hermes_bin or "찾을 수 없음(ALPHRED_HERMES_BIN)")
    # 2) Hermes API (:8642)
    h = _ping(f"{cfg.api_base_url}/models")
    rep["hermes_api"] = h
    add("Hermes API (:8642)", h["up"],
        f"{cfg.api_base_url} → {'응답' if h['up'] else h.get('error') or h.get('status')}")
    # 3) Alphred 게이트웨이 (:8643)
    g = _ping(f"{cfg.gateway_url}/")
    rep["gateway"] = g
    add("Alphred 게이트웨이 (:8643)", g["up"],
        f"{cfg.gateway_url} → {'응답' if g['up'] else g.get('error') or g.get('status')}")
    # 4) 모델/provider (config 읽기 — 호출 없음)
    try:
        from .gateway import _read_model_cfg
        mc = _read_model_cfg(cfg)
        model = mc.get("default") or "(미설정)"
        provider = mc.get("provider") or (model.split("/")[0] if "/" in model else "?")
        rep["model"] = {"default": mc.get("default"), "provider": mc.get("provider")}
        add("모델/provider", bool(mc.get("default")), f"{model}  (provider={provider})")
    except Exception as e:
        add("모델/provider", False, f"config 읽기 실패: {e}")
    # 5) 플래너/LLM 분류 플래그
    add("플래너(ALPHRED_PLANNER)", True, "ON" if cfg.planner else "OFF")
    add("LLM 분류(ALPHRED_LLM_CLASSIFY)", True, "ON" if cfg.llm_classify else "OFF")
    add("산출물 검증(ALPHRED_VERIFY)", True, "ON (Tier0 결정적)" if cfg.verify else "OFF")
    add("수용 judge(ALPHRED_JUDGE)", True,
        f"ON (Tier2 LLM, high, 재시도≤{cfg.judge_max_retries})" if cfg.judge else "OFF (쿼터 절약)")
    # 6) 큐 상태 (게이트웨이 가동 시) / DB 직접
    counts: dict = {}
    if g["up"]:
        try:
            tasks = httpx.get(f"{cfg.gateway_url}/queue",
                              headers=_auth_headers(cfg), timeout=3.0).json().get("tasks", [])
            for t in tasks:
                counts[t["state"]] = counts.get(t["state"], 0) + 1
        except Exception:
            pass
    if not counts:
        try:
            from .db import Store
            store = Store(cfg.db_path)
            try:
                for t in store.list():
                    counts[t.state] = counts.get(t.state, 0) + 1
            finally:
                store.close()
        except Exception:
            pass
    rep["queue"] = counts
    add("큐", True, ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(비어 있음)")
    # 7) 검증 통계(§21 V3) — DB 직접 집계(LLM 호출 없음)
    try:
        import json as _json

        from .db import Store
        store = Store(cfg.db_path)
        try:
            rows = store.list()
        finally:
            store.close()
        completed = sum(1 for t in rows if t.state == "Completed")
        needs = sum(1 for t in rows if t.state == "NeedsReview")
        finished = completed + needs
        depth_dist: dict = {}
        retried: list = []
        scores: list = []
        for t in rows:
            if getattr(t, "depth", None):
                depth_dist[t.depth] = depth_dist.get(t.depth, 0) + 1
            if (getattr(t, "verify_attempts", 0) or 0) > 0:
                retried.append(t.verify_attempts)
            if getattr(t, "verify_report", None):
                try:
                    j = (_json.loads(t.verify_report) or {}).get("judge")
                    if isinstance(j, dict) and isinstance(j.get("score"), (int, float)):
                        scores.append(j["score"])
                except Exception:
                    pass
        rep["verify_stats"] = {"completed": completed, "needs_review": needs,
                               "depth": depth_dist, "judge_scores": scores}
        rate = f"{completed}/{finished} ({100 * completed // finished}%)" if finished else "N/A"
        detail = f"통과율 {rate} · 검토필요 {needs}"
        if depth_dist:
            detail += " · 심화도 " + ",".join(f"{k}:{v}" for k, v in sorted(depth_dist.items()))
        if retried:
            detail += f" · 평균 재시도 {sum(retried) / len(retried):.1f}"
        if scores:
            detail += f" · judge 평균 {sum(scores) / len(scores):.0f}"
        add("검증 통계(§21)", True, detail)
    except Exception as e:
        add("검증 통계(§21)", True, f"집계 불가: {e}")
    # 8) 안전망(재시작 가드)
    try:
        from .safety import RestartGuard
        guard = RestartGuard(cfg.guard_path, cfg.restart_window_seconds, cfg.restart_threshold)
        add("안전망(#30719)", not guard.tripped(),
            f"재시작 {guard.count()}회/{cfg.restart_window_seconds:.0f}s (임계 {cfg.restart_threshold})")
    except Exception as e:
        add("안전망(#30719)", False, str(e))
    return rep


def _auth_headers(cfg: Config) -> dict:
    return {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}


def _print_doctor(rep: dict) -> None:
    print("Alphred doctor — 런타임 점검\n")
    for c in rep["checks"]:
        mark = "[OK] " if c["ok"] else "[!!] "
        print(f"  {mark}{c['name']:28} {c['detail']}")
    print()
    print("종합: " + ("정상" if rep["ok"] else "주의 — 위 [!!] 항목 확인 필요"))
    if not rep.get("hermes_api", {}).get("up"):
        print("  · Hermes API 미응답: `alphred serve`(자동 기동) 또는 Hermes 게이트웨이 확인.")
    if not rep.get("gateway", {}).get("up"):
        print("  · 게이트웨이 미응답: `alphred serve` 로 :8643 기동.")


def _cmd_tui(argv: list[str]) -> int:
    """전용 Alphred TUI(Textual) — 게이트웨이(:8643) 터미널 클라이언트(기획 §13)."""
    no_daemon = "--no-daemon" in argv
    try:
        from .tui import run_tui
    except Exception as e:
        sys.stderr.write(
            f"alphred: TUI 의존성을 불러올 수 없습니다 ({e}). `pip install textual` 후 다시 시도하세요.\n"
            "  (또는 `alphred chat` 으로 Hermes TUI 를 쓰거나 `alphred queue list` 로 큐를 확인하세요.)\n")
        return 1
    cfg = Config.load()
    if not no_daemon and "ALPHRED_NO_DAEMON" not in os.environ:
        _ensure_daemon()  # :8643 Alphred 서비스 보장(없으면 백그라운드 기동)
    run_tui(cfg.gateway_url, cfg.api_key, cfg.alphred_home)
    return 0


def _cmd_serve(argv: list[str]) -> int:
    import argparse
    from .gateway import serve
    p = argparse.ArgumentParser(prog="alphred serve", description="Alphred 게이트웨이 기동")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8643)
    p.add_argument("--interval", type=float, default=1.0, help="스케줄러 tick 주기(초)")
    p.add_argument("--no-auto-hermes", action="store_true",
                   help="Hermes API 게이트웨이 자동 기동을 끄고 외부 인스턴스를 사용")
    args = p.parse_args(argv)
    serve(host=args.host, port=args.port, interval=args.interval,
          auto_hermes=not args.no_auto_hermes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
