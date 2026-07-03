/*
 * Alphred ESP32 client — §35.9 접속 매트릭스 모드 e (최소 예제, 회로 불필요)
 *
 * 하는 일: WiFi 접속 → Alphred 게이트웨이(/v1/chat/completions)에 질문 POST →
 *   · HTTP 200 (Light)  : 즉답을 시리얼 모니터에 출력
 *   · HTTP 202 (Heavy)  : 작업이 백그라운드 큐로 감 → /v1/runs/{id} 를 폴링해 결과 출력
 *     (폴링 대신 서버 푸시를 원하면 요청 body 에 "delivery":{"webhook":"http://<수신기>"} 지정)
 *
 * 임베디드 특성과 Alphred 설계의 정합: API 소스 요청은 착수 전 질문(needs_input)을 받지
 * 않는다 — 기기가 질문에 답할 수 없으므로 Alphred 가 합리적 가정을 기록하고 진행한다(§34.4).
 *
 * 준비물:
 *   1) 서버: alphred serve --host 0.0.0.0   (외부 바인딩은 키 필수)
 *   2) 키:   alphred keys issue esp32-거실   → 아래 ALPHRED_KEY 에 붙여넣기
 *   3) Arduino IDE: ESP32 보드 지원 + ArduinoJson 라이브러리 설치
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ---- 설정 ----
const char* WIFI_SSID  = "YOUR_WIFI";
const char* WIFI_PASS  = "YOUR_PASS";
const char* ALPHRED    = "http://192.168.0.10:8643";   // alphred serve 주소
const char* ALPHRED_KEY = "alph_xxxxxxxxxxxxxxxx";      // alphred keys issue 로 발급
const char* QUESTION   = "오늘 서울 날씨 한 줄로 알려줘";  // 짧으면 즉답(Light)

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print("."); }
  Serial.printf("\n연결됨: %s\n", WiFi.localIP().toString().c_str());
  ask(QUESTION);
}

void loop() { delay(60000); }   // 데모: 부팅 시 1회 질문

void ask(const char* text) {
  HTTPClient http;
  http.begin(String(ALPHRED) + "/v1/chat/completions");
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + ALPHRED_KEY);
  http.setTimeout(60000);   // Light 응답은 모델 속도에 따라 수십 초 걸릴 수 있음

  JsonDocument req;
  req["messages"][0]["role"] = "user";
  req["messages"][0]["content"] = text;
  String body;
  serializeJson(req, body);

  int code = http.POST(body);
  String resp = http.getString();
  http.end();

  if (code == 200) {                                   // Light — 즉답
    JsonDocument d;
    if (deserializeJson(d, resp) == DeserializationError::Ok) {
      const char* answer = d["choices"][0]["message"]["content"];
      Serial.printf("[답변] %s\n", answer ? answer : resp.c_str());
    }
  } else if (code == 202) {                            // Heavy — 큐 등록 → 폴링
    JsonDocument d;
    deserializeJson(d, resp);
    const char* id = d["id"] | d["run_id"] | "";
    Serial.printf("[큐 등록] task=%s — 결과 폴링 시작\n", id);
    pollRun(id);
  } else if (code == 401) {
    Serial.println("[오류] 인증 실패 — 서버에서 alphred keys issue 로 키를 발급하세요");
  } else {
    Serial.printf("[오류] HTTP %d: %s\n", code, resp.c_str());
  }
}

void pollRun(const char* id) {
  for (int i = 0; i < 90; i++) {                       // 최대 ~15분 (10s 간격)
    delay(10000);
    HTTPClient http;
    http.begin(String(ALPHRED) + "/v1/runs/" + id);
    http.addHeader("Authorization", String("Bearer ") + ALPHRED_KEY);
    int code = http.GET();
    String resp = http.getString();
    http.end();
    if (code != 200) { Serial.printf("[폴링 오류] HTTP %d\n", code); return; }
    JsonDocument d;
    deserializeJson(d, resp);
    const char* status = d["status"] | "?";
    Serial.printf("[상태] %s\n", status);
    if (strcmp(status, "completed") == 0) {
      const char* out = d["output"] | "(결과 없음)";
      bool review = d["needs_review"] | false;
      Serial.printf("[완료%s] %s\n", review ? "·검토필요" : "", out);
      return;
    }
    if (strcmp(status, "cancelled") == 0) { Serial.println("[취소됨]"); return; }
  }
  Serial.println("[폴링 시간 초과] — 대시보드/TUI 에서 확인하세요");
}
