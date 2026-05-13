# Public proxy + Codex deployment

This repository includes a small public-auth wrapper and OpenAI Responses API support so the same service can be used by both Claude Code and Codex.

## What is included

- `cp4cc.py`
  - `POST /v1/messages` for Claude Code / Anthropic-compatible clients.
  - `POST /v1/responses` for Codex / OpenAI Responses API clients.
  - Converts Anthropic `max_tokens` to OpenAI `max_completion_tokens` when non-Claude models are routed through `/chat/completions`.
- `cp4cc_auth_proxy.py`
  - Imports `cp4cc.app` and adds a shared-secret middleware.
  - `/health` is public.
  - All other endpoints require either:
    - `Authorization: Bearer <CP4CC_PUBLIC_TOKEN>`, or
    - `X-Api-Key: <CP4CC_PUBLIC_TOKEN>`.

## Install on a new machine

```bash
git clone https://github.com/satomic/copilot-4-claude-code.git
cd copilot-4-claude-code
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Authenticate the proxy with GitHub Copilot once:

```bash
python cp4cc.py --port 8092
```

Complete the GitHub device-flow prompt. After the token is cached in `.github_copilot_token/`, stop the process and run the protected wrapper.

## Environment file

Create `.env` on the target machine; do not commit it:

```bash
CP4CC_PUBLIC_TOKEN=replace-with-a-long-random-secret
CP4CC_PORT=8092
PYTHONUNBUFFERED=1
```

Generate a token, for example:

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

## systemd service

Example unit file: `/etc/systemd/system/copilot-4-claude-code.service`

```ini
[Unit]
Description=GitHub Copilot to Anthropic/OpenAI API Proxy (cp4cc)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/copilot-4-claude-code
EnvironmentFile=/opt/copilot-4-claude-code/.env
ExecStart=/opt/copilot-4-claude-code/.venv/bin/uvicorn cp4cc_auth_proxy:app --host 127.0.0.1 --port 8092 --log-level warning
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Adjust the paths if you clone somewhere else, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now copilot-4-claude-code
sudo systemctl status copilot-4-claude-code --no-pager
```

## nginx reverse proxy

Example public location:

```nginx
location /copilot/ {
    client_max_body_size 20m;
    proxy_pass http://127.0.0.1:8092/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

`client_max_body_size 20m` matters for Codex because its `/v1/responses` request body can grow after multiple agent turns.

Reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Verify

```bash
BASE="https://your-domain.example/copilot"
TOKEN="replace-with-your-CP4CC_PUBLIC_TOKEN"

curl -sS "$BASE/health"

curl -sS "$BASE/v1/models" \
  -H "Authorization: Bearer $TOKEN" | head

curl -sS "$BASE/v1/responses" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"Reply OK","max_output_tokens":20,"stream":false}'
```

## Claude Code client

Set the base URL to the proxy root, not `/v1/messages`:

```bash
export ANTHROPIC_BASE_URL="https://your-domain.example/copilot"
export ANTHROPIC_API_KEY="replace-with-your-CP4CC_PUBLIC_TOKEN"
claude --model claude-sonnet-4.6
```

Alternatively use `~/.claude/settings.json`:

```json
{
  "apiKeyHelper": "echo replace-with-your-CP4CC_PUBLIC_TOKEN"
}
```

## Codex client

Configure `~/.codex/config.toml`:

```toml
model = "gpt-5.5"
model_provider = "copilot-proxy"
model_reasoning_effort = "high"

[model_providers.copilot-proxy]
name = "Copilot Proxy"
base_url = "https://your-domain.example/copilot/v1"
env_key = "COPILOT_PROXY_API_KEY"
wire_api = "responses"
```

Run:

```bash
export COPILOT_PROXY_API_KEY="replace-with-your-CP4CC_PUBLIC_TOKEN"
codex exec "Reply exactly OK"
```

## Notes

- A proxy that only exposes `/v1/messages` is enough for Claude Code but not for modern Codex.
- Modern Codex custom providers require `wire_api = "responses"` and call `POST /v1/responses`.
- Keep `.env`, `.github_copilot_token/`, and `logs/` private.
