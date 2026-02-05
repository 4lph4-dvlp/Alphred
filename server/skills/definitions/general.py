import os
from skills.base import Skill

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "general"
        self.description = "General purpose assistant"
        self.system_prompt = "You are a helpful assistant. Use provided tools when necessary."
        # Initially empty. We can add some basic tools later if needed.
        self.mcp_servers = []
        
        # 1. Notion MCP configuration
        # Check for local build first
        notion_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../mcp_servers/notion-mcp-server/build/src/index.js"))
        if os.path.exists(notion_path):
            self.mcp_servers.append({
                "command": "node",
                "args": [notion_path],
                "env": {"NOTION_API_KEY": os.getenv("NOTION_API_KEY", "")}
            })
            
        # 2. Brave Search MCP (via Smithery)
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
