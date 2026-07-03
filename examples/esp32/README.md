# Alphred × ESP32 (임베디드 클라이언트 예제)

§35.9 접속 매트릭스 **모드 e** — ESP32(또는 WiFi 지원 Arduino 계열)에서 Alphred 서버에
질문을 보내고 결과를 받는 최소 예제. **회로 불필요** — 시리얼 모니터만으로 데모 가능.

## 동작 방식

1. WiFi 접속 → `POST /v1/chat/completions` (OpenAI 호환, Bearer 키)
2. **HTTP 200** = Light(즉답) → 답변을 시리얼에 출력
3. **HTTP 202** = Heavy(백그라운드 큐) → `GET /v1/runs/{id}` 10초 간격 폴링 → 완료 시 결과 출력
   - 폴링 없이 받으려면 요청에 `"delivery": {"webhook": "http://<수신기>/hook"}` 를 넣으면
     종결 시 Alphred 가 결과를 POST 해준다(§35.2)

> **임베디드와 인테이크 질문**: 기기는 질문(needs_input)에 답할 수 없다.
> Alphred 는 API 소스 요청에는 질문하지 않고 합리적 가정을 기록해 진행하도록 설계되어
> 있어(§34.4) 임베디드에서 그대로 안전하다.

## 준비

```bash
# 서버(호스트 머신)에서:
alphred keys issue esp32-거실          # 키 발급(한 번만 표시 — 스케치에 붙여넣기)
alphred serve --host 0.0.0.0           # 외부 바인딩(키 발급 후에만 기동 허용)
```

Arduino IDE: **ESP32 보드 패키지** + **ArduinoJson** 라이브러리 설치 →
`alphred_client/alphred_client.ino` 의 WIFI/서버 주소/키 4줄 수정 → 업로드 →
시리얼 모니터(115200).

## 상태

코드 제공·로직은 서버측 API 와 계약 일치 확인. **실기기 컴파일/실행은 미실측**
(Arduino 툴체인 부재 환경에서 작성) — 실기기 검증 시 이 줄을 갱신할 것.
