# Phase 0 PoC — Hermes 선점/재개 primitive 실증

Alphred의 핵심(선점형 큐)이 실물 Hermes 위에서 성립하는지 **자격증명·게이트웨이 가동 후** 실증한다.

## 무엇을 검증하나
| ID | 검증 | 왜 중요한가 |
|----|------|------------|
| T1 | `/v1/runs` 비동기 실행 → `run_id` 즉시 수신 | Heavy 작업을 큐에 비동기로 던지는 토대 |
| T2 | `/v1/runs/{id}/events` SSE | 진행률/도구 실시간 스트리밍 |
| T3 | `/v1/runs/{id}/stop` | **일시중지(선점)** 의 토대 |
| T4-A | `/v1/responses` + `previous_response_id` 재개 | Hermes 내장 재개 경로 |
| T4-B | `conversation_history` 패스스루 재개 | **Alphred SSOT 재개(권장 가설)** |

> 정적 분석 결과 `/v1/runs`는 재개용 `response_id`를 저장하지 않음을 확인했다.
> 따라서 stop 이후 재개는 T4-B(Alphred가 conversation_history 보유)가 더 신뢰성 높을 것으로 가설을 세웠고, 이 PoC가 A/B를 실측 비교한다.

## 사전 준비 (사용자 수행 필요 — 비용/로그인 동반)

이 머신은 Hermes 설정이 `C:\Users\alpha\AppData\Local\hermes\` 에 있으며 **LLM 자격증명이 미설정** 상태다.

1. **LLM 자격증명 설정** (택1):
   ```powershell
   hermes secrets        # 대화형으로 OpenRouter/Anthropic/Gemini 등 키 입력
   # 또는
   hermes login          # Nous Portal 관리형 모델 사용
   ```
2. **API 서버 가동** (별도 터미널, 세션에서는 `! ` 접두사로 실행 가능):
   ```powershell
   $env:API_SERVER_ENABLED="true"
   $env:API_SERVER_KEY="dev-local-key"
   hermes gateway
   ```
   기본 포트 **8642**.

## 실행
```powershell
python poc/verify_primitives.py --base-url http://localhost:8642/v1 --api-key dev-local-key
```

## 합격 기준
- T1~T3 PASS → 비동기 실행 + 중단(선점) primitive 확인.
- T4-A 또는 T4-B 중 **하나 이상** PASS → 재개 경로 확보. (둘 다 결과를 기록해 Phase 2 설계의 기준으로 삼는다.)
