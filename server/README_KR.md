# Alphred Server & Worker

[![English Version](https://img.shields.io/badge/Language-English-red)](README.md)

이 디렉토리는 Alphred V3의 백엔드 로직을 포함합니다. FastAPI 서버(**Concierge**)와 백그라운드 실행 루프(**Worker**)가 이곳에 있습니다.

## 🧩 아키텍처 구성 요소

### 1. Concierge (Server.py)
서비스의 "얼굴" 역할을 합니다. 특정 엔드포인트(`/chat`)를 통해 사용자와 대화합니다.
-   **역할**: 사용자 의도 파악, 대화 관리, 작업(Task) 위임.
-   **스킬**: `TaskManagementSkill`을 사용하여 데이터베이스와 상호작용합니다.
-   **제약**: 직접 복잡한 작업을 실행하지 않도록 설계되었습니다.

### 2. Worker (Worker.py)
서비스의 "손과 발" 역할을 합니다. 사용자와 직접 대화하지 않습니다.
-   **역할**: 작업 실행, 에러 핸들링, 결과 보고.
-   **스킬**: `GeneralSkill` (또는 전문 스킬)을 사용하며 강력한 MCP 도구들을 장착합니다.
-   **루프**: Supabase에서 `PENDING` 작업을 주기적으로 가져와 실행하고 결과를 업데이트합니다.

### 3. Skill 매니저 & MCP 클라이언트
-   **`mcp_client/`**: 표준 MCP 프로토콜 구현체입니다. 로컬 서버들과 `stdio` 방식으로 통신합니다.
-   **`skills/`**: "Skill"을 관리합니다. 하나의 Skill은 특정 시스템 프롬프트와 MCP 도구 세트의 조합입니다.
-   **`mcp_servers/`**: 로컬 MCP 서버 구현체를 저장하는 공간입니다.

## 🛠 환경 설정 (`.env`)

이 디렉토리에 `.env` 파일을 생성하고 다음 키들을 설정하세요:

```ini
# 데이터베이스 (Supabase)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SECRET_KEY=your-service-role-key

# 인증 토큰 (클라이언트와 일치해야 함)
ALPHRED_ACCESS_TOKEN=your-client-secret-token

# LLM 공급자 ('groq/', 'openai/', 'gemini/' 등으로 시작)
DEFAULT_MODEL=groq/llama-3.3-70b-versatile

# 임베딩 (Gemini 추천)
GEMINI_API_KEY=your-gemini-key
```

## 🏃 실행 가이드

### Windows (PowerShell)
```powershell
# 터미널 1: Concierge
python server.py

# 터미널 2: Worker
python worker.py
```

### macOS / Linux
```bash
# 터미널 1: Concierge
python3 server.py

# 터미널 2: Worker
python3 worker.py
```

## 🔌 개발자 가이드

### 새로운 MCP 서버 추가 방법
Alphred는 **표준 Model Context Protocol**을 따릅니다. 존재하는 모든 표준 MCP 서버를 연결할 수 있습니다.

1.  **서버 준비**:
    -   `mcp_servers/<server_name>`에 코드를 두거나,
    -   `npm` 또는 `pip`로 글로벌 설치된 명령어를 사용합니다.
2.  **등록**:
    -   사용하려는 스킬의 정의 파일(예: `skills/definitions/general.py`)을 수정합니다.
    -   `mcp_servers` 리스트에 설정을 추가합니다.

```python
# general.py 예시
self.mcp_servers = [
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/Work"]
    }
]
```

### 새로운 Skill 추가 방법
Skill은 Alphred가 컨텍스트(예: 일반 작업 -> 전문 코딩 작업)를 전환하게 해줍니다.

1.  **정의 생성**: `server/skills/definitions/my_skill.py` 파일을 생성합니다.
2.  **상속**: `skills.base.Skill`을 상속받습니다.
3.  **정의**: `name`, `description`, `system_prompt`, `mcp_servers`를 설정합니다.
4.  **등록**: `SkillManager`가 로드할 수 있도록 설정합니다.

```python
class MySkill(Skill):
    def __init__(self):
        super().__init__()
        self.name = "coding_expert"
        self.system_prompt = "당신은 시니어 파이썬 개발자입니다..."
        self.mcp_servers = [...] # 코딩 관련 도구들
```

## ✅ 표준 준수
이 프로젝트는 다음 표준을 엄격히 준수합니다:
-   **MCP Specification**: JSON-RPC 2.0 및 stdio 통신 표준 사용.
-   **OpenAI Tool Format**: MCP 도구 스키마를 OpenAI 호환 JSON으로 자동 변환.
