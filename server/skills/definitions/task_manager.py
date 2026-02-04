from typing import Dict, Any, List
from skills.base import Skill
from supabase import create_client
import os

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "task_manager"
        self.description = "Manages tasks in the task database"
        self.system_prompt = (
            "You are a Concierge Agent.\n"
            "Your goal is to understand user requests and create TASKS for the Worker Agent if they involve file operations, coding, or complex execution.\n"
            "- Use 'create_task' to delegate work.\n"
            "- Use 'list_tasks' to check progress.\n"
            "- Do NOT try to execute code yourself. Always delegate."
        )
        self.mcp_servers = [] # This skill uses direct DB access, not an external MCP server for now.
        
        # Init DB client for this skill
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SECRET_KEY")
        self.db = create_client(url, key)

    async def create_task(self, title: str, description: str) -> str:
        """Creates a new task for the Worker."""
        try:
            data = {"title": title, "description": description, "status": "pending"}
            res = self.db.table("tasks").insert(data).execute()
            return f"Task created successfully. ID: {res.data[0]['id']}"
        except Exception as e:
            return f"Error creating task: {str(e)}"

    async def list_tasks(self, status: str = None) -> str:
        """Lists tasks. Optional status filter: pending, in_progress, completed, failed."""
        try:
            query = self.db.table("tasks").select("id, title, status, result").order("created_at", desc=True).limit(10)
            if status:
                query = query.eq("status", status)
            res = query.execute()
            
            if not res.data:
                return "No tasks found."
            
            summary = "\n".join([f"[{t['status'].upper()}] {t['title']} (ID: {t['id']})" for t in res.data])
            return summary
        except Exception as e:
            return f"Error listing tasks: {str(e)}"
            
    async def get_tools(self) -> List[Dict[str, Any]]:
        """Returns the tools for this skill manually since it's a native skill."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "create_task",
                    "description": "Delegate a new task to the Worker Agent.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short summary of the task"},
                            "description": {"type": "string", "description": "Detailed step-by-step instructions for the Worker"}
                        },
                        "required": ["title", "description"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_tasks",
                    "description": "Check the status of recent tasks.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "failed"], "description": "Filter by status"}
                        }
                    }
                }
            }
        ]

    # Override dispatch to handle local methods
    async def dispatch_local(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "create_task":
            return await self.create_task(**args)
        elif name == "list_tasks":
            return await self.list_tasks(**args)
        return None
