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
Alphred follows the **Standard Model Context Protocol**. You can add any standard MCP server.

1.  **Local Server**:
    -   Place the code in `mcp_servers/<server_name>`.
    -   Or install via npm/pip globally.
2.  **Registering**:
    -   Update the corresponding Skill definition (e.g., `skills/definitions/general.py`).
    -   Add the server config to the `mcp_servers` list in the Skill class.

```python
# Example in general.py
self.mcp_servers = [
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/Work"]
    }
]
```

### How to Add a New Skill
Skills allow Alphred to switch contexts (e.g., from General Helper to specialized Coder).

1.  **Create Definition**: Create `server/skills/definitions/my_skill.py`.
2.  **Inherit**: Inherit from `skills.base.Skill`.
3.  **Define**: Set `name`, `description`, `system_prompt`, and `mcp_servers`.
4.  **Register**: Update `skills/manager.py` (if using a static registry) or rely on dynamic loading (if implemented).

```python
class MySkill(Skill):
    def __init__(self):
        super().__init__()
        self.name = "coding_expert"
        self.system_prompt = "You are a senior python developer..."
        self.mcp_servers = [...] # Coding specific tools
```

## ✅ Standard Compliance
This project strictly adheres to:
-   **MCP Specification**: Uses standard JSON-RPC 2.0 via stdio for tool communication.
-   **OpenAI Tool Format**: Automatically converts MCP tool schemas to OpenAI-compatible JSON.