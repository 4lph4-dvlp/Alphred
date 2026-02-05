import os
from skills.base import Skill

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "general"
        self.description = "General purpose assistant"
        self.system_prompt = "You are a helpful assistant. Use provided tools when necessary."
        # Initially empty. We can add some basic tools later if needed.
        # Initially empty. We can add specific tools using separate skill files.
        self.mcp_servers = []
