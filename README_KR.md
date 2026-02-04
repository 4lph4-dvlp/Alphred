# Alphred V3: 듀얼 에이전트 지능형 비서

[![English Version](https://img.shields.io/badge/Language-English-red)](README.md)

Alphred V3는 대화형 안내와 본격적인 작업 수행을 분리한 최첨단 **듀얼 에이전트(Dual-Agent) AI 비서**입니다. **Model Context Protocol (MCP)**과 **Skill 아키텍처**를 활용하여 강력하고 확장 가능한 자율 작업 흐름을 제공합니다.

## 🏗 아키텍처 (Architecture)

Alphred V3는 두 개의 독립적인 에이전트와 공유 상태 데이터베이스를 사용하는 **매니저-워커(Manager-Worker)** 패턴으로 작동합니다:

1.  **Concierge Agent (안내자)**:
    *   사용자와 직접 소통합니다.
    *   의도를 파악하고 맥락을 관리합니다.
    *   복잡한 작업은 '작업 데이터베이스'를 통해 Worker에게 위임합니다.
    *   *도구*: 기억(Memory) 접근, 작업 관리(Create/List).

2.  **Worker Agent (작업자)**:
    *   백그라운드에서 실행됩니다.
    *   대기 중인 작업을 감지하여 처리합니다.
    *   실질적인 작업(코딩, 파일 조작, 웹 검색 등)을 수행합니다.
    *   *도구*: 전체 MCP 제품군 (Filesystem, Git, Brave Search 등).

3.  **Task Database (Supabase)**:
    *   두 에이전트 간의 소통 다리 역할을 합니다.
    *   비동기 작업의 상태(`대기` -> `진행 중` -> `완료`)를 추적합니다.

## 🚀 설치 및 설정 (Installation)

### 필수 요구 사항
-   **Python**: 3.10 이상
-   **Node.js**: 18+ (Filesystem 등 일부 표준 MCP 서버 실행에 필요)
-   **Supabase 계정**: 기억(Memory) 및 작업(Task) 저장소용.

### 1. 저장소 복제
```bash
git clone https://github.com/your-repo/alphred.git
cd alphred
```

### 2. 데이터베이스 설정 (Supabase)
Supabase SQL Editor에서 제공된 스크립트를 실행하여 필요한 테이블(`memories`, `tasks`, `user_profile`)을 생성하세요.
-   스크립트 위치: [`server/setup_v3.sql`](server/setup_v3.sql)

### 3. 서버 설정
서버 디렉토리로 이동하여 의존성 라이브러리를 설치합니다.

**Windows (PowerShell)**
```powershell
cd server
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
```

**macOS / Linux**
```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. 환경 설정
`server` 디렉토리에 `.env` 파일을 생성합니다. 상세 설정(API Key, DB URL 등)은 `server/README_KR.md`를 참고하세요.

## 🏃 실행 방법 (Running)

전체 시스템을 가동하려면 **Concierge 서버**와 **Worker 프로세스**를 각각 실행해야 합니다.

**터미널 1: Concierge 서버**
```bash
# /server 디렉토리 내부
python server.py
# 서버가 http://localhost:8000 에서 시작됩니다.
```

**터미널 2: Worker 에이전트**
```bash
# /server 디렉토리 내부
python worker.py
# Worker가 작업을 기다리기 시작합니다...
```

## 📚 문서 (Documentation)
-   [**서버 문서 (Server)**](server/README_KR.md): 상세 아키텍처, MCP 서버/Skill 추가 방법, API 가이드.
-   [**CLI 클라이언트 문서**](client/cli/README_KR.md): 커맨드 라인 인터페이스 사용법.

---
**Alphred** - *당신의 지능형 코파일럿.*
