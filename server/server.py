import os
import json
import datetime
import warnings
import logging
import logging
from contextlib import asynccontextmanager
from collections import deque

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
from litellm import completion, embedding
import litellm
from prompts import get_system_prompt

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

# --- Alphred 메모리 엔진 ---

# --- Alphred 메모리 엔진 ---

class AlphredMemory:
    @staticmethod
    def format_memory_content(timestamp, role, content) -> str:
        """
        Formats memory unified: [YYYY-MM-DD HH:MM:ss] Role: Content
        Parses ISO timestamp if string.
        """
        try:
            if isinstance(timestamp, str):
                dt = datetime.datetime.fromisoformat(timestamp)
            else:
                dt = timestamp
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            ts_str = str(timestamp)
            
        # Normalize role for display
        display_role = "User" if role.lower() == "user" else "AI"
        return f"[{ts_str}] {display_role}: {content}"

    @staticmethod
    def initialize_cache():
        try:
            res = supabase.table("memories").select("role", "content", "created_at").order("created_at", desc=True).limit(50).execute()
            for h in res.data[::-1]:
                role = "user" if h['role'] in ["User", "user"] else "assistant"
                # STM Format: [Time] Role: Content
                formatted_content = AlphredMemory.format_memory_content(h['created_at'], role, h['content'])
                AlphredMemory.short_term_cache.append({"role": role, "content": formatted_content})
        except Exception as e:
            print(f"[Memory Init Error] {e}")

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
            for m in res.data:
                # LTM Format: [Time] Role: Content
                line = AlphredMemory.format_memory_content(m['created_at'], m['role'], m['content'])
                ctx += f"- {line}\n"
            return ctx
        except: return ""

    @staticmethod
    def store(role, content):
        # Database: Store raw content
        now = datetime.datetime.now()
        vec = AlphredMemory.get_embedding(content)
        
        # Cache: Store formatted content
        cache_role = "user" if role in ["User", "user"] else "assistant"
        formatted_content = AlphredMemory.format_memory_content(now, cache_role, content)
        AlphredMemory.short_term_cache.append({"role": cache_role, "content": formatted_content})
        
        if vec:
            try: supabase.table("memories").insert({
                "role": role, 
                "content": content,  # Raw content in DB
                "embedding": vec, 
                "created_at": now.isoformat()
            }).execute()
            except: pass

# --- API 서버 설정 ---

from skills.manager import SkillManager
skill_manager = SkillManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    AlphredMemory.initialize_cache()
    await skill_manager.activate_skill("task_manager")
    yield
    await skill_manager.shutdown()

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
    active_skill = skill_manager.get_active_skill()
    skill_prompt = active_skill.get_system_prompt() if active_skill else ""
    
    system_msg = get_system_prompt(lt_ctx, skill_prompt)
    
    messages = [{"role": "system", "content": system_msg}]
    messages.extend(list(AlphredMemory.short_term_cache))
    messages.append({"role": "user", "content": user_input})

    try:
        # Get dynamic tools from active skill
        tools = await skill_manager.get_tools()
        
        response = completion(
            model=Config.DEFAULT_MODEL,
            messages=messages,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
            fallbacks=[Config.GEMINI_MODEL]
        )
        
        msg = response.choices[0].message
        
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            messages.append(msg)
            for tool in msg.tool_calls:
                name = tool.function.name
                # Note: Arguments are already JSON string in OpenAI format, but litellm wrapper might give dict?
                # LiteLLM/OpenAI usually gives string in arguments.
                args = json.loads(tool.function.arguments)
                mcp_log.append(name)
                
                result = await skill_manager.dispatch_tool_call(name, args)
                messages.append({"tool_call_id": tool.id, "role": "tool", "name": name, "content": str(result)})
            
            final_res = completion(model=Config.DEFAULT_MODEL, messages=messages)
            answer = final_res.choices[0].message.content
        else:
            answer = msg.content

        AlphredMemory.store("User", user_input)
        AlphredMemory.store("AI", answer)
        return ChatResponse(reply=answer, long_term_searched=is_lt, mcp_used=mcp_log)
    except Exception as e:
        # print error stacktrace for debugging
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)