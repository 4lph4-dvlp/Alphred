import asyncio
import os
import importlib.util
from typing import Dict, List, Any, Optional
from skills.base import Skill
from mcp_client.session import MCPClientSession
from contextlib import AsyncExitStack

class SkillManager:
    """
    Manages loading of Skills and their associated MCP clients.
    """
    def __init__(self):
        self.skills: Dict[str, Skill] = {}
        self.active_skill: Optional[Skill] = None
        self.exit_stack = AsyncExitStack()
        self.active_sessions: List[MCPClientSession] = []
        self._load_skills()

    def _load_skills(self):
        """
        Loads skill definitions from server/skills/definitions.
        """
        definitions_dir = os.path.join(os.path.dirname(__file__), "definitions")
        if not os.path.exists(definitions_dir):
            os.makedirs(definitions_dir, exist_ok=True)
            return

        for filename in os.listdir(definitions_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                try:
                    name = filename[:-3]
                    spec = importlib.util.spec_from_file_location(name, os.path.join(definitions_dir, filename))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    
                    if hasattr(module, "SkillImpl"):
                        skill_instance = module.SkillImpl()
                        self.skills[name] = skill_instance
                        print(f"[SkillManager] Loaded skill: {name}")
                except Exception as e:
                    print(f"[SkillManager] Failed to load skill {filename}: {e}")
        
        # Set default active skill if available
        if "general" in self.skills:
            self.active_skill = self.skills["general"]

    async def activate_skill(self, skill_name: str):
        """
        Activates a skill and connects to its MCP servers.
        """
        if skill_name not in self.skills:
            return False
            
        # 1. Cleanup previous sessions
        await self.shutdown()
        
        # 2. Set new active skill
        self.active_skill = self.skills[skill_name]
        self.exit_stack = AsyncExitStack()
        self.active_sessions = []
        
        # 3. Connect to all MCP servers defined in the skill
        print(f"[SkillManager] Activating skill: {skill_name}")
        for server_config in self.active_skill.get_mcp_servers():
            try:
                session = MCPClientSession(
                    command=server_config["command"],
                    args=server_config.get("args", []),
                    env=server_config.get("env", None)
                )
                # Enter context properly
                await self.exit_stack.enter_async_context(session.connect())
                self.active_sessions.append(session)
            except Exception as e:
                print(f"[SkillManager] Failed to connect to server {server_config}: {e}")
                
    async def get_tools(self) -> List[Dict[str, Any]]:
        """
        Returns a list of tools from all active sessions in OpenAI format.
        """
        all_tools = []
        
        # 1. Add local tools if any
        if hasattr(self.active_skill, 'get_tools'):
             # If get_tools is async
            if asyncio.iscoroutinefunction(self.active_skill.get_tools):
                local_tools = await self.active_skill.get_tools()
            else:
                 local_tools = self.active_skill.get_tools()
            # Avoid dupes if base class returns empty list but not method override
            if local_tools:
                all_tools.extend(local_tools)

        # 2. Add MCP tools
        for session in self.active_sessions:
            try:
                tools = await session.list_tools_openai_format()
                all_tools.extend(tools)
            except Exception as e:
                print(f"[SkillManager] Error listing tools: {e}")
        return all_tools
        
    async def dispatch_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Message routing: Finds the server that has this tool and executes it.
        """
        # 1. Check if it's a local native tool (e.g. TaskManager)
        if hasattr(self.active_skill, 'dispatch_local'):
            result = await self.active_skill.dispatch_local(tool_name, arguments)
            if result is not None:
                return result

        # 2. Check active MCP sessions
        for session in self.active_sessions:
            try:
                # Optimization: We should cache which session has which tool.
                # For now, we list tools to check if it exists in this session.
                # This causes overhead but ensures correctness.
                result = await session.session.list_tools()
                for tool in result.tools:
                    if tool.name == tool_name:
                        return await session.call_tool(tool_name, arguments)
            except Exception as e:
                continue
                
        return f"Error: Tool '{tool_name}' not found in active skill sessions."

    async def shutdown(self):
        """
        Closes all active sessions.
        """
        if self.exit_stack:
            await self.exit_stack.aclose()
        self.active_sessions = []
