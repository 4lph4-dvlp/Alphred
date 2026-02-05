# Alphred Server & Worker

[![Korean Version](https://img.shields.io/badge/Language-Korean-blue)](README_KR.md)

This directory contains the backend logic for Alphred V3. It hosts the FastAPI server (**Concierge**) and the background execution loop (**Worker**).

## 🧩 Architecture Components

### 1. Concierge (Server.py)
The Concierge is the "Face" of Alphred. It interacts with the user via specific endpoints (`/chat`).
-   **Role**: Intent recognition, conversation management, task delegation.
-   **Skill**: Uses `TaskManagementSkill` to interact with the database.
-   **Identity**: Aware that it cannot execute tasks directly.

### 2. Worker (Worker.py)
The Worker is the "Hands" of Alphred. It has no direct user interface.
-   **Role**: Task execution, error handling, result reporting.
-   **Skill**: Uses `GeneralSkill` (or domain-specific skills) equipped with heavy MCP tools.
-   **Loop**: Polls Supabase for `PENDING` tasks -> Executes -> Updates result.

### 3. Skill Manager & MCP Client
-   **`mcp_client/`**: Implements the Standard MCP Protocol. Connects to `stdio` based local servers.
-   **`skills/`**: Manages "Skills". A Skill resolves to a specific System Prompt and a set of MCP Tools.
-   **`mcp_servers/`**: Location for local MCP server implementations.

## 🛠 Configuration (`.env`)

Create a `.env` file in this directory with the following keys:

```ini
# Database (Supabase)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SECRET_KEY=your-service-role-key

# Authentication
ALPHRED_ACCESS_TOKEN=your-client-secret-token

# LLM Provider (Start with 'groq/', 'openai/', or 'gemini/')
DEFAULT_MODEL=groq/llama-3.3-70b-versatile
# DEFAULT_MODEL=openai/gpt-4-turbo

# Embedding (Gemini recommended)
GEMINI_API_KEY=your-gemini-key
```

## 🏃 Execution Guide (Linux/macOS)

### 5.1. Start Concierge (Server)
```bash
source ./venv/bin/activate
nohup uvicorn server:app --host 0.0.0.0 --port 8000 &
tail -f nohup.out
# Check for "Application startup complete"
```

### 5.2. Start Worker
```bash
nohup python worker.py > worker.log 2>&1 &
tail -f worker.log
```

### 5.3. Stop Services
```bash
# Find PIDs
ps -ef | grep uvicorn
ps -ef | grep worker.py

# Kill process
kill -9 [PID]
```

### Windows (PowerShell)
```powershell
.\venv\Scripts\Activate
python server.py
# Open a new terminal
python worker.py
```

## 🔌 Developer Guide

### How to Add a New MCP Server

Alphred strictly follows the **Standard Model Context Protocol**. You can add any standard MCP server using either a local source or a remote package manager like Smithery.

#### 1. Adding a Local MCP Server (Example: Notion)

**Assumption**: You want to run the [Notion MCP Server](https://github.com/makenotion/notion-mcp-server) from source.

1.  **Download & Build**:
    ```bash
    # Go to the configuration directory
    cd server
    mkdir -p mcp_servers
    cd mcp_servers
    
    # Clone the repository
    git clone https://github.com/makenotion/notion-mcp-server.git
    cd notion-mcp-server
    
    # Install dependencies and build
    npm install
    npm run build
    
    # Verify the output (Usually in build/src/index.js or dist/index.js)
    ls build/src/index.js
    ```

2.  **Configure in a Skill**:
    Refer to "How to Add a New Skill (Modular Approach)" below to add this server to a skill file.

#### 2. Adding a Remote MCP Server (Example: Brave Search via Smithery)

**Assumption**: You want to use [Brave Search](https://smithery.ai/server/@modelcontextprotocol/server-brave-search) managed by Smithery.

1.  **Prerequisite**: Ensure you have a valid API Key.
2.  **Command Construction**:
    Smithery allows running servers directly via `npx`.
    Command: `npx`
    Args: `["-y", "@smithery/cli", "run", "@modelcontextprotocol/server-brave-search", "--config", "{\"braveApiKey\": \"YOUR_KEY\"}"]`

### How to Add a New Skill (Modular Approach)

Instead of editing a single giant file, create separate definition files for each Skill. The `SkillManager` automatically loads all `.py` files in `server/skills/definitions/`.

#### Example 1: Notion Skill (`server/skills/definitions/notion_skill.py`)
This skill uses the **Local MCP** we built above.

```python
import os
from skills.base import Skill

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "notion_assistant"
        self.description = "Manages Notion pages and content."
        self.system_prompt = "You are a comprehensive Notion assistant. Help users manage their workspace."
        self.mcp_servers = []
        
        # Absolute path to the locally built server
        # Adjust 'build/src/index.js' based on actual build output
        server_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../mcp_servers/notion-mcp-server/build/src/index.js"))
        
        if os.path.exists(server_path):
            self.mcp_servers.append({
                "command": "node",
                "args": [server_path],
                "env": {
                    "NOTION_API_KEY": os.getenv("NOTION_API_KEY")
                }
            })
```

#### Example 2: Search Skill (`server/skills/definitions/search_skill.py`)
This skill uses the **Remote Brave Search MCP** via Smithery.

```python
import os
from skills.base import Skill

class SkillImpl(Skill):
    def __init__(self):
        super().__init__()
        self.name = "web_searcher"
        self.description = "Searches the web for real-time information."
        self.system_prompt = "You are a researcher. Use Brave Search to find accurate info."
        self.mcp_servers = []
        
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
```

**Note**: After creating these files, restart the server. The `SkillManager` will see `notion_assistant` and `web_searcher` and load them automatically.

## ✅ Standard Compliance
This project strictly adheres to:
-   **MCP Specification**: Uses standard JSON-RPC 2.0 via stdio for tool communication.
-   **OpenAI Tool Format**: Automatically converts MCP tool schemas to OpenAI-compatible JSON.