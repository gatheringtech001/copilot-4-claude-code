# copilot-4-claude-code

[English](README.md) | [中文](README.zh.md)

A local proxy server that exposes **GitHub Copilot** as a standard **Anthropic API**, enabling tools like [Claude Code](https://claude.ai/code) to use it as a backend.

## How It Works

```
Claude Code  →  Anthropic API (localhost:8082)  →  GitHub Copilot API
```

1. The proxy runs a FastAPI server locally on port `8082`
2. Claude Code sends requests to the proxy using the standard Anthropic `/v1/messages` format
3. The proxy authenticates with GitHub Copilot and forwards requests appropriately:
   - **Claude models** → direct passthrough to Copilot's `/v1/messages` endpoint (no conversion needed)
   - **Other models** (GPT-4o, etc.) → converted from Anthropic format to OpenAI format and sent to `/chat/completions`
   - **Codex / Responses API clients** → passthrough via `/v1/responses`

## Authentication Flow

1. GitHub OAuth Device Flow → `access_token`
2. `access_token` → Copilot API key via `https://api.github.com/copilot_internal/v2/token`
3. Copilot API key + required headers → GitHub Copilot API requests

Tokens are cached locally in `.github_copilot_token/` and auto-refreshed on expiry.

## Requirements

- Python 3.10+
- A GitHub account with an active **GitHub Copilot** subscription

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Start the proxy

```bash
python cp4cc.py
```

On first run, you will be prompted to authenticate via GitHub OAuth Device Flow:

```
==================================================
  Visit: https://github.com/login/device
  Auth code: >>>  XXXX-XXXX  <<<
==================================================
```

Once authenticated, the proxy starts on port `8082`.

### Startup options

| Flag | Description |
|---|---|
| *(none)* | Bind to `127.0.0.1`, UI and audit enabled |
| `--share` | Bind to `0.0.0.0` — LAN accessible ⚠ |
| `--fast` | Disable UI and audit for lower overhead |
| `--port N` | Listen on port N (default: 8082) |

```bash
python cp4cc.py --share          # share with LAN
python cp4cc.py --fast           # proxy only, no UI
python cp4cc.py --fast --share   # both
python cp4cc.py --port 9000      # custom port
python cp4cc.py --help           # full help
```

### Configure Claude Code

**Option 1: Persistent environment variables**
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8082
export ANTHROPIC_AUTH_TOKEN=dummy
claude
```

**Option 2: Single launch**
```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8082 ANTHROPIC_AUTH_TOKEN=dummy claude
```

> If using `--share` or `--port`, replace the host/port accordingly.

## Dashboard

A web dashboard is available at **http://localhost:8082/ui** with a responsive two-column layout:

**Left panel — Models**
- Lists all available Copilot models with their supported endpoints
- Claude models (direct `/v1/messages` passthrough) are grouped separately from other models

**Right panel — Requests (current session)**
- Live request list showing time, model, status, duration, message type breakdown, and a content preview
- Message types are color-coded: `msg` (blue) · `tool↑` tool_use (orange) · `tool↓` tool_result (purple)
- Preview column shows the last message's text; for tool messages the full body (tool name + input / result) is displayed
- List auto-refreshes every 5 seconds; only current session data is shown

The header strip also provides one-click copy of the Claude Code configuration commands and live stats (total / OK / error counts).

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Health check |
| `GET /v1/models` | List available models (Anthropic format) |
| `POST /v1/messages` | Main proxy endpoint (Anthropic format) |
| `POST /v1/responses` | OpenAI Responses API endpoint for Codex |
| `GET /v1/models/refresh` | Force refresh model list |
| `GET /audit/sessions` | List all audit session files |
| `GET /audit/current` | Current session audit log (JSON) |
| `GET /ui` | Web dashboard |

## Model Name Mapping

Claude Code model names are automatically mapped to Copilot format:

| Claude Code | Copilot |
|---|---|
| `claude-opus-4-6` | `claude-opus-4.6` |
| `claude-opus-4-6-20250514` | `claude-opus-4.6` |
| `claude-haiku-4-5` | `claude-haiku-4.5` |
| `gpt-4o` | `gpt-4o` (unchanged) |

## Logging & Audit

- Application logs: `logs/app.log`
- Per-session audit logs: `logs/audit/session_YYYYMMDD_HHMMSS_<id>.json`

Each audit entry captures: original model, mapped Copilot model, endpoint, stream flag, per-message breakdown (role / type / full body), status code, duration, and response body (up to 2000 chars for streamed responses).

## Project Structure

```
.
├── cp4cc.py             # Proxy server (FastAPI)
├── requirements.txt     # Python dependencies
├── logs/
│   ├── app.log          # Application log
│   └── audit/           # Per-session audit JSON files
└── .github_copilot_token/
    ├── access-token     # GitHub OAuth access token
    └── api-key.json     # Copilot API key (auto-refreshed)
```
