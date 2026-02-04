# Alphred V3: Dual-Agent Intelligent Assistant

[![Korean Version](https://img.shields.io/badge/Language-Korean-blue)](README_KR.md)

Alphred V3 is a cutting-edge **Dual-Agent AI Assistant** designed to separate conversational guidance from heavy task execution. It leverages the **Model Context Protocol (MCP)** and a **Skill Architecture** to provide a robust, extensible, and efficient autonomous workflow.

## 🏗 Architecture

Alphred V3 operates on a **Manager-Worker** pattern involving two distinct agents and a shared state database:

1.  **Concierge Agent (The Interface)**:
    *   Interacts with the user.
    *   Understands intents and manages the context.
    *   Delegates complex work to the Worker via the Task Database.
    *   *Tools*: Memory Access, Task Management (Create/List).

2.  **Worker Agent (The Executor)**:
    *   Runs in the background.
    *   Polls the Task Database for pending jobs.
    *   Executes heavy operations (Coding, File I/O, Web Search).
    *   *Tools*: Full MCP Suite (Filesystem, Git, Brave Search, etc.).

3.  **Task Database (Supabase)**:
    *   Acts as the communication bridge.
    *   Tracks the status (`Pending` -> `In Progress` -> `Completed`) of asynchronous tasks.

## 🚀 Installation & Setup

### Prerequisites
-   **Python**: 3.10 or higher
-   **Node.js**: 18+ (Required for some standard MCP servers like Brave Search or Filesystem)
-   **Supabase Account**: For Memory and Task database.

### 1. Clone the Repository
```bash
git clone https://github.com/your-repo/alphred.git
cd alphred
```

### 2. Database Setup (Supabase)
Run the provided SQL script in your Supabase SQL Editor to create the necessary tables (`memories`, `tasks`, `user_profile`).
-   Script Path: [`server/setup_v3.sql`](server/setup_v3.sql)

### 3. Server Setup
Navigate to the server directory and install dependencies.

**Windows (PowerShell)**
```powershell
cd server
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
```

**macOS / Linux**
```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Configuration
Create a `.env` file in the `server` directory. Refer to `server/README.md` for detailed environment variable settings (API Keys, DB URL, etc.).

## 🏃 Running Alphred

To run the full system, you need to start both the **Concierge Server** and the **Worker** process.

**Terminal 1: Concierge Server**
```bash
# Inside /server
python server.py
# Server runs on http://localhost:8000
```

**Terminal 2: Worker Agent**
```bash
# Inside /server
python worker.py
# Worker starts polling for tasks...
```

## 📚 Documentation
-   [**Server Documentation**](server/README.md): Detailed architecture, adding MCP servers/Skills, and API usage.
-   [**CLI Client Documentation**](client/cli/README.md): How to use the command-line interface.

---
**Alphred** - *Your Intelligent Co-Pilot.*
