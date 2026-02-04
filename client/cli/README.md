# Alphred CLI Client

[![Korean Version](https://img.shields.io/badge/Language-Korean-blue)](README_KR.md)

This directory contains the Command Line Interface (CLI) client for interacting with the Alphred Concierge Server.

## 🏗 Architecture
The CLI is a lightweight 'Dummy Terminal' that acts as the primary input/output device for the User.
-   **Protocol**: HTTP/REST (POST `/chat`)
-   **Auth**: Bearer Token / Custom Header (`x-alphred-token`)
-   **Role**: Sends user messages to the Concierge -> Displays the response.

## ⚙️ Configuration

Create a `.env` file or configure your environment variables:

```ini
# Server URL
ALPHRED_SERVER_URL=http://localhost:8000

# Access Token (Must match server config)
ALPHRED_ACCESS_TOKEN=your-client-secret-token
```

## 🖥 Usage

### Starting the Client

```bash
# If it's a python script (example)
python client.py
```

*(Note: Depending on the implementation language (Python/Go/Rust), the start command may vary.)*

### Interactive Mode
Once started, you can type messages naturally.
-   **User**: "Clean my desktop"
-   **Alphred**: "I've created a task (#101) for the Worker. I'll let you know when it's done."

### Commands
-   `/exit` or `/quit`: Exit the client.
-   `/clear`: Clear the screen.
