import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class MCPClientSession:
    """
    Manages a single MCP server connection via stdio.
    """
    def __init__(self, command: str, args: List[str] = None, env: Dict[str, str] = None):
        self.command = command
        self.args = args or []
        self.env = env or os.environ.copy()
        self.session: Optional[ClientSession] = None
        self._exit_stack = None

    @asynccontextmanager
    async def connect(self):
        """
        Connects to the MCP server and yields the session.
        """
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                self.session = session
                yield self

    async def list_tools_openai_format(self) -> List[Dict[str, Any]]:
        """
        Lists tools from the server and converts them to OpenAI format.
        """
        if not self.session:
            raise RuntimeError("MCP Session not initialized")
            
        result = await self.session.list_tools()
        openai_tools = []
        
        for tool in result.tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            })
            
        return openai_tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """
        Calls a tool on the MCP server.
        """
        if not self.session:
            raise RuntimeError("MCP Session not initialized")
            
        result = await self.session.call_tool(name, arguments)
        return result.content
