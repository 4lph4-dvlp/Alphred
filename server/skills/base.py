from typing import List, Dict, Any, Optional

class Skill:
    """
    Base class for Alphred Skills.
    A Skill defines a context (Prompt) and a set of Tools (MCP Servers).
    """
    def __init__(self):
        self.name: str = "Base Skill"
        self.description: str = ""
        self.system_prompt: str = ""
        self.mcp_servers: List[Dict[str, Any]] = []
        # Structure of mcp_servers dict:
        # {
        #   "command": "npx",
        #   "args": ["-y", "@modelcontextprotocol/server-filesystem", "..."],
        #   "env": {...} (Optional)
        # }

    def get_system_prompt(self) -> str:
        return self.system_prompt

    def get_mcp_servers(self) -> List[Dict[str, Any]]:
        return self.mcp_servers
