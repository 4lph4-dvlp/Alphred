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

## 🏃 실행 및 종료 방법 (Linux/macOS)

### 5.1. 실행 방법
**Step 1: Concierge (서버) 실행**
```bash
source ./venv/bin/activate
nohup uvicorn server:app --host 0.0.0.0 --port 8000 &
tail -f nohup.out
# "Application startup complete" 메시지가 나오면 성공
```

**Step 2: Worker (에이전트) 실행**
```bash
nohup python worker.py > worker.log 2>&1 &
tail -f worker.log
```

### 5.2. 종료 방법
```bash
# 프로세스 확인
ps -ef | grep uvicorn
ps -ef | grep worker.py

# 종료
kill -9 [PID]
```

### Windows (PowerShell)
```powershell
.\venv\Scripts\Activate
python server.py
# 새 터미널 열기
python worker.py
```

## 🔌 개발자 가이드

### 새로운 MCP 서버 추가 방법

Alphred는 **표준 Model Context Protocol**을 따릅니다. 로컬 소스 또는 Smithery 같은 원격 패키지 매니저를 통해 MCP 서버를 추가할 수 있습니다.

#### 1. 로컬 MCP 서버 추가 (예시: Notion)

**가정**: [Notion MCP Server](https://github.com/makenotion/notion-mcp-server) 소스코드를 직접 받아 실행하려 합니다.

1.  **다운로드 및 빌드**:
    ```bash
    # Alphred 서버 디렉토리 기준
    cd server
    mkdir -p mcp_servers
    cd mcp_servers
    
    # 리포지토리 클론
    git clone https://github.com/makenotion/notion-mcp-server.git
    cd notion-mcp-server
    
    # 의존성 설치 및 빌드
    npm install
    npm run build
    
    # 빌드 결과 확인 (보통 build/src/index.js 또는 dist/index.js)
    ls build/src/index.js
    ```

2.  **스킬에 설정하기**:
    아래 "새로운 Skill 추가 방법 (모듈식 접근)"을 참고하여 설정합니다.

#### 2. 원격 MCP 서버 추가 (예시: Brave Search via Smithery)

**가정**: Smithery를 통해 [Brave Search](https://smithery.ai/server/@modelcontextprotocol/server-brave-search)를 실행하려 합니다.

1.  **준비물**: 유효한 Brave API Key가 필요합니다.
2.  **명령어 구성**:
    `npx`를 사용하여 Smithery CLI를 실행합니다.
    Command: `npx`
    Args: `["-y", "@smithery/cli", "run", "@modelcontextprotocol/server-brave-search", "--config", "{\"braveApiKey\": \"YOUR_KEY\"}"]`

### 새로운 Skill 추가 방법 (모듈식 접근)

하나의 거대한 파일(`general.py`)을 수정하는 대신, 각 스킬별로 독립적인 파일을 생성하는 방식을 권장합니다. `SkillManager`는 `server/skills/definitions/` 폴더 내의 모든 `.py` 파일을 자동으로 로드합니다.

#### 예시 1: Notion Skill (`server/skills/definitions/notion_skill.py`)
앞서 빌드한 **로컬 Notion MCP**를 사용하는 스킬입니다.

```python
import os
from skills.base import Skill

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "notion_assistant"
        self.description = "노션 페이지와 콘텐츠를 관리합니다."
        self.system_prompt = "당신은 노션 전문가입니다. 사용자의 워크스페이스 관리를 돕습니다."
        self.mcp_servers = []
        
        # 로컬 빌드된 서버의 절대 경로
        server_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../mcp_servers/notion-mcp-server/build/src/index.js"))
        
        if os.path.exists(server_path):
            self.mcp_servers.append({
                "command": "node",
                "args": [server_path],
                "env": {
                    "NOTION_API_KEY": os.getenv("NOTION_API_KEY")
                }
            })
```

#### 예시 2: 검색 Skill (`server/skills/definitions/search_skill.py`)
Smithery를 통해 **원격 Brave Search MCP**를 사용하는 스킬입니다.

```python
import os
from skills.base import Skill

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "web_searcher"
        self.description = "실시간 웹 정보를 검색합니다."
        self.system_prompt = "당신은 웹 리서처입니다. Brave Search를 통해 정확한 정보를 찾으세요."
        self.mcp_servers = []
        
        brave_key = os.getenv("BRAVE_API_KEY")
        if brave_key:
            self.mcp_servers.append({
                "command": "npx",
                "args": [
                    "-y",
                    "@smithery/cli",
                    "run",
                    "@modelcontextprotocol/server-brave-search",
                    "--config",
                    f'{{"braveApiKey": "{brave_key}"}}'
                ]
            })
```

**참고**: 파일을 생성한 후 서버를 재시작하면, `SkillManager`가 자동으로 `notion_assistant`와 `web_searcher` 스킬을 인식하고 로드합니다.

## ✅ 표준 준수
이 프로젝트는 다음 표준을 엄격히 준수합니다:
-   **MCP Specification**: JSON-RPC 2.0 및 stdio 통신 표준 사용.
-   **OpenAI Tool Format**: MCP 도구 스키마를 OpenAI 호환 JSON으로 자동 변환.
