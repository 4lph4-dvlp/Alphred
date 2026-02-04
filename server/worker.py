import asyncio
import os
import logging
from supabase import create_client
from dotenv import load_dotenv
from litellm import completion
from skills.manager import SkillManager
from prompts import get_system_prompt

# 1. Setup
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [WORKER] - %(message)s')
logger = logging.getLogger("worker")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "groq/llama-3.3-70b-versatile")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
skill_manager = SkillManager()

async def process_task(task):
    task_id = task['id']
    logger.info(f"Processing Task {task_id}: {task['title']}")
    
    try:
        # 1. Update status to IN_PROGRESS
        supabase.table("tasks").update({"status": "in_progress"}).eq("id", task_id).execute()
        
        # 2. Setup Context (Activate Skill)
        # For now, Worker uses 'general' skill or we can determine skill from task type.
        # Let's use 'general' which should have the Filesystem/Git tools if configured.
        # Currently 'general' is empty, BUT we will assume it gets populated with tools later.
        # For this demo, we assume general has tools.
        await skill_manager.activate_skill("general")
        
        # 3. Build Prompt
        system_msg = (
            "You are the Worker Agent for Alphred.\n"
            "Your job is to execute the following task accurately and efficiently using available tools.\n"
            "Report the final result clearly."
        )
        task_prompt = f"TASK: {task['title']}\nDETAILS: {task['description']}"
        
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": task_prompt}
        ]
        
        tools = await skill_manager.get_tools()
        
        # 4. LLM Execution Loop (Simple Single-Turn or Multi-Turn)
        # We need a loop to handle tool calls.
        MAX_TURNS = 10
        final_result = ""
        
        for _ in range(MAX_TURNS):
            response = completion(
                model=DEFAULT_MODEL,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None
            )
            
            msg = response.choices[0].message
            messages.append(msg)
            
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tool in msg.tool_calls:
                    name = tool.function.name
                    import json
                    args = json.loads(tool.function.arguments)
                    logger.info(f"Tool Call: {name} {args}")
                    
                    result = await skill_manager.dispatch_tool_call(name, args)
                    messages.append({"tool_call_id": tool.id, "role": "tool", "name": name, "content": str(result)})
            else:
                final_result = msg.content
                break
        
        # 5. Complete Task
        supabase.table("tasks").update({
            "status": "completed", 
            "result": final_result,
            "updated_at": "now()"
        }).eq("id", task_id).execute()
        logger.info(f"Task {task_id} Completed.")
        
    except Exception as e:
        logger.error(f"Task Failed: {e}")
        supabase.table("tasks").update({
            "status": "failed", 
            "result": str(e),
            "updated_at": "now()"
        }).eq("id", task_id).execute()

async def worker_loop():
    logger.info("Worker started. Polling for tasks...")
    while True:
        try:
            # Poll for pending tasks
            res = supabase.table("tasks").select("*").eq("status", "pending").order("created_at", desc=False).limit(1).execute()
            
            if res.data:
                await process_task(res.data[0])
            else:
                await asyncio.sleep(5) # Wait before next poll
                
        except Exception as e:
            logger.error(f"Loop Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(worker_loop())
