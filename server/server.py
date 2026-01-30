import os
import json
import datetime
import warnings
import logging
import importlib
from contextlib import asynccontextmanager
from collections import deque

import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
from litellm import completion, embedding
import litellm

# 1. 시스템 설정 및 경고 억제
load_dotenv()
warnings.filterwarnings("ignore")
logging.getLogger("litellm").setLevel(logging.ERROR)
litellm.telemetry = False
litellm.drop_params = True

class Config:
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
    ACCESS_TOKEN = os.getenv("ALPHRED_ACCESS_TOKEN")
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "groq/llama-3.3-70b-versatile")
    GEMINI_MODEL = "gemini/gemini-1.5-flash"
    EMBEDDING_MODEL = "gemini/text-embedding-004"
    SEARCH_THRESHOLD = 0.35

# Supabase 클라이언트 초기화
supabase: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_SECRET_KEY)

# --- MCP 매니저 (플러그인 및 외부 서버 관리) ---

class MCPManager:
    def __init__(self, config_path="mcp/mcp_config.json"):
        self.config_path = config_path
        self.registry = {}
        self.specs = []
        self.load_mcp_apps()

    def load_mcp_apps(self):
        if not os.path.exists(self.config_path):
            print(" [경고] mcp/mcp_config.json 파일이 없습니다.")
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            for name, info in config.items():
                spec_path = os.path.join("mcp", "specs", info["spec_file"])
                if os.path.exists(spec_path):
                    with open(spec_path, "r", encoding="utf-8") as sf:
                        spec = json.load(sf)
                        self.specs.append(spec)
                
                if info["type"] == "local":
                    module = importlib.import_module(info["module"])
                    self.registry[name] = {"type": "local", "func": module.execute}
                elif info["type"] == "external":
                    self.registry[name] = {"type": "external", "url": info["url"]}
        except Exception as e:
            print(f" [오류] MCP 로드 중 에러: {e}")

    def dispatch(self, name: str, args: dict):
        target = self.registry.get(name)
        if not target: return f"오류: '{name}' 도구를 찾을 수 없습니다."

        if target["type"] == "local":
            try: return target["func"](**args)
            except Exception as e: return f"로컬 플러그인 에러: {str(e)}"
        else:
            try:
                with httpx.Client(timeout=15.0) as client:
                    r = client.post(target["url"], json={"arguments": args})
                    return r.json().get("result", "응답 없음")
            except Exception as e: return f"외부 서버 에러: {str(e)}"

# --- Alphred 메모리 엔진 ---

class AlphredMemory:
    short_term_cache = deque(maxlen=50)

    @staticmethod
    def initialize_cache():
        try:
            res = supabase.table("memories").select("role", "content").order("created_at", desc=True).limit(50).execute()
            for h in res.data[::-1]:
                role = "user" if h['role'] == "User" else "assistant"
                AlphredMemory.short_term_cache.append({"role": role, "content": h['content']})
        except: pass

    @staticmethod
    def get_embedding(text):
        try:
            res = embedding(model=Config.EMBEDDING_MODEL, input=[text])
            return res.data[0]['embedding']
        except: return None

    @staticmethod
    def retrieve_long_term(query):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            it_res = completion(
                model=Config.GEMINI_MODEL,
                messages=[{"role": "system", "content": f"현재 시간 {now}. 시간범위를 JSON으로 분석."},
                          {"role": "user", "content": query}],
                response_format={"type": "json_object"}
            )
            it = json.loads(it_res.choices[0].message.content)
            vec = AlphredMemory.get_embedding(query)
            if not vec: return ""
            res = supabase.rpc("match_memories", {
                "query_embedding": vec, "match_threshold": Config.SEARCH_THRESHOLD, "match_count": 5,
                "from_date": it.get("from_date", "-infinity"), "to_date": it.get("to_date", "infinity")
            }).execute()
            if not res.data: return ""
            ctx = "\n[관련된 장기 기억 기록]\n"
            for m in res.data: ctx += f"- ({m['created_at']}) {m['role']}: {m['content']}\n"
            return ctx
        except: return ""

    @staticmethod
    def store(role, content):
        cache_role = "user" if role == "User" else "assistant"
        AlphredMemory.short_term_cache.append({"role": cache_role, "content": content})
        vec = AlphredMemory.get_embedding(content)
        if vec:
            try: supabase.table("memories").insert({"role": role, "content": content, "embedding": vec, "created_at": datetime.datetime.now().isoformat()}).execute()
            except: pass

# --- API 서버 설정 ---

mcp_manager = MCPManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    AlphredMemory.initialize_cache()
    yield

app = FastAPI(title="Alphred API v3.1", lifespan=lifespan)

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    reply: str
    long_term_searched: bool
    mcp_used: list = []

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, x_alphred_token: str = Header(None)):
    if not x_alphred_token or x_alphred_token != Config.ACCESS_TOKEN:
        raise HTTPException(status_code=403, detail="Unauthorized")

    user_input = request.message
    mcp_log = []
    
    # 1. 기억 및 컨텍스트 준비
    lt_ctx = AlphredMemory.retrieve_long_term(user_input)
    is_lt = len(lt_ctx) > 0
    
    # 2. **업그레이드된 시스템 프롬프트 (High-Level Persona)**
    system_msg = (
        "## 당신의 정체성\n"
        "당신은 'Alphred'입니다. 단순한 챗봇이 아니라, 사용자의 삶을 이해하고 함께 성장하는 지능형 비서이자 협력적 파트너입니다.\n\n"
        
        "## 대화 원칙\n"
        "1. **맥락적 통찰**: 제공된 [기억 기록]과 [대화 내역]을 백과사전식으로 나열하지 마세요. 현재 질문에 필요한 정보만 선별하여 자연스럽게 인용하세요.\n"
        "2. **입체적 답변**: 답변은 간결하되 풍부한 내용을 담아야 합니다. 사용자의 질문 뒤에 숨은 의도를 파악하고, 필요하다면 보조적인 정보나 조언을 함께 제공하세요.\n"
        "3. **주도적 리딩**: 대화를 단순히 끝내지 마세요. 답변 끝에 사용자의 프로젝트(Alphred, the_watcher 등)나 관심사에 기반한 질문, 혹은 논리적인 다음 단계를 제안하세요.\n"
        "4. **톤앤매너**: 통찰력 있고, 지적이며, 때로는 가벼운 위트를 섞어 대화하세요. 비판보다는 지원적이고 긍정적인 자세를 유지합니다.\n\n"
        
        "## 도구(MCP) 사용 가이드\n"
        "- 제공된 도구들은 당신의 능력을 확장하는 수단입니다. 외부 데이터가 반드시 필요하거나 실시간 정보가 요구될 때만 전략적으로 호출하세요.\n"
        "- 불필요한 도구 호출은 피하되, 호출이 결정되었다면 실행 결과를 답변의 근거로 명확히 활용하세요.\n\n"
        
        "당신은 이 모든 문맥을 이해하고 있는 최고의 파트너임을 잊지 마세요."
    )
    if lt_ctx: system_msg += f"\n\n[관련 장기 기억 참고]:\n{lt_ctx}"
    
    messages = [{"role": "system", "content": system_msg}]
    messages.extend(list(AlphredMemory.short_term_cache))
    messages.append({"role": "user", "content": user_input})

    try:
        response = completion(
            model=Config.DEFAULT_MODEL,
            messages=messages,
            tools=mcp_manager.specs if mcp_manager.specs else None,
            tool_choice="auto" if mcp_manager.specs else None,
            fallbacks=[Config.GEMINI_MODEL]
        )
        
        msg = response.choices[0].message
        
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            messages.append(msg)
            for tool in msg.tool_calls:
                name = tool.function.name
                args = json.loads(tool.function.arguments)
                mcp_log.append(name)
                result = mcp_manager.dispatch(name, args)
                messages.append({"tool_call_id": tool.id, "role": "tool", "name": name, "content": str(result)})
            
            final_res = completion(model=Config.DEFAULT_MODEL, messages=messages)
            answer = final_res.choices[0].message.content
        else:
            answer = msg.content

        AlphredMemory.store("User", user_input)
        AlphredMemory.store("AI", answer)
        return ChatResponse(reply=answer, long_term_searched=is_lt, mcp_used=mcp_log)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)