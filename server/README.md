## 0. 준비
---
GCP 무료 VM 인스턴스 생성 필요

## 1. VM 방화벽
---
VPC 네트워크 → 방화벽 → 방화벽 규칙 만들기

| 이름 |  |
| --- | --- |
| 설명 |  |
| 로그 | 사용 안 함 |
| 네트워크 | default |
| 우선순위 | 1000 |
| 트래픽 방향 | 인그레스 (ingress) |
| 일치 시 작업 | 허용 |
| 대상 | 지정된 대상 태그 |
| 대상 태그 | alphred-server |
| 소스 필터 | IPv4 범위 |
| 소스 IPv4 범위 | 0.0.0.0/0 (모든 IP 허용) |
| 보조 소스 필터 |  |
| 대상 필터 |  |
| 프로토콜 및 포트 | 지정된 프로토콜 및 포트 |
|  | TCP |
|  | 8000 |

Compute Engine → VM 인스턴스 → [설정할 대상 태그] → 수정

네트워크 태그에 대상 태그로 설정한 태그 추가

## 2. Superbase DB 설계 및 설정
---
### 2.1. 계정 생성
github 계정 연동

### 2.2. 프로젝트 생성
| Organization | 4lph4-dvlp |
| --- | --- |
| Project name | Alphred |
| Database password | 4lphr3d1574mb3573n! |
| Region | Asia-Pacific |

### 2.3. pgvector 활성화
Database → Extentions → 검색창에 vector 검색 → 활성화

### 2.4. 테이블 설계
SQL Editor → Private → Untitled File

```sql
-- 1. 확장 기능 활성화 (벡터 연산용)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 기존 테이블 및 함수 초기화 (Clean Slate)
DROP FUNCTION IF EXISTS match_memories;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS user_profile;

-- 3. 장기 기억 테이블 생성 (Gemini 768차원 최적화)
CREATE TABLE memories (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz DEFAULT now(),
  role text NOT NULL,        -- 'User' 또는 'AI'
  content text NOT NULL,     -- 대화 내용
  embedding vector(768)      -- Gemini text-embedding-004 임베딩 데이터
);

-- 4. 사용자 프로필 테이블 생성
CREATE TABLE user_profile (
  id int8 PRIMARY KEY DEFAULT 1,
  name text,
  preferences jsonb DEFAULT '{"model_priority": ["groq/llama-3.3-70b-versatile", "gemini/gemini-1.5-flash"]}',
  updated_at timestamptz DEFAULT now()
);

-- 5. 기본 사용자 데이터 삽입
INSERT INTO user_profile (id, name) VALUES (1, '알파');

-- 6. 고속 검색을 위한 벡터 인덱스 생성
CREATE INDEX ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- 7. [핵심] 시간 필터링이 포함된 벡터 검색 함수 등록
CREATE OR REPLACE FUNCTION match_memories (
  query_embedding vector(768),
  match_threshold float,
  match_count int,
  from_date timestamptz DEFAULT '-infinity',
  to_date timestamptz DEFAULT 'infinity'
)
RETURNS TABLE (
  id uuid,
  content text,
  role text,
  created_at timestamptz,
  similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    memories.id,
    memories.content,
    memories.role,
    memories.created_at,
    1 - (memories.embedding <=> query_embedding) AS similarity
  FROM memories
  WHERE (1 - (memories.embedding <=> query_embedding) > match_threshold)
    AND (memories.created_at >= from_date)
    AND (memories.created_at <= to_date)
  ORDER BY memories.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;
```

이후 Run으로 실행행

### 2.5. API 키 확인
Project Settings → API Keys
Publishable key와 Secret keys 확인

### 2.6. 데이터 조작
**모든 데이터 삭제**
```sql
DELETE FROM memories;
```

**특정 ID 데이터 삭제**
```sql
DELETE FROM memories WHERE id = 'uuid-값';
```

**특정 날짜 이전 데이터 삭제**
```sql
DELETE FROM memories WHERE created_at < '2026-01-01';
```

**AI가 대답한 내용만 삭제**
```sql
DELETE FROM memories WHERE role = 'AI';
```

**특정 단어가 포함된 기록 삭제**
```sql
DELETE FROM memories WHERE content LIKE '%비밀번호%';
```

## 3. 실행 및 종료 방법
---
### 3.1. 실행 방법
```bash
source ./venv/bin/activate
nohup uvicorn server:app --host 0.0.0.0 --port 8000 &
tail -f nohup.out #Application startup complete 나오면 됨
```
### 3.2. 종료 방법
```bash
ps -ef | grep uvicorn
kill -[ps로 구한 PID]
```