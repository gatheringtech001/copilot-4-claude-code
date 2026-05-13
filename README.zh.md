# copilot-4-claude-code

[English](README.md) | [中文](README.zh.md)

一个本地代理服务，将 **GitHub Copilot** 包装成标准的 **Anthropic API**，使 [Claude Code](https://claude.ai/code) 等工具能够以 Copilot 作为后端运行。

## 工作原理

```
Claude Code  →  Anthropic API (localhost:8082)  →  GitHub Copilot API
```

1. 代理在本地 `8082` 端口启动一个 FastAPI 服务
2. Claude Code 以标准 Anthropic `/v1/messages` 格式发送请求
3. 代理完成认证后将请求转发给 GitHub Copilot：
   - **Claude 模型** → 直接透传到 Copilot 的 `/v1/messages` 接口（无需格式转换）
   - **其他模型**（GPT-4o 等）→ 从 Anthropic 格式转换为 OpenAI 格式，发送到 `/chat/completions`
   - **Codex / Responses API 客户端** → 通过 `/v1/responses` 透传

## 认证流程

1. GitHub OAuth Device Flow → `access_token`
2. `access_token` → 通过 `https://api.github.com/copilot_internal/v2/token` 获取 Copilot API key
3. Copilot API key + 必要请求头 → 调用 GitHub Copilot API

Token 缓存在本地 `.github_copilot_token/` 目录，过期后自动刷新。

## 环境要求

- Python 3.10+
- 拥有有效 **GitHub Copilot** 订阅的 GitHub 账号

## 安装

```bash
pip install -r requirements.txt
```

## 使用

### 启动代理

```bash
python cp4cc.py
```

首次运行时，会提示通过 GitHub OAuth Device Flow 完成认证：

```
==================================================
  Visit: https://github.com/login/device
  Auth code: >>>  XXXX-XXXX  <<<
==================================================
```

认证完成后，代理将在 `8082` 端口启动。

### 启动参数

| 参数 | 说明 |
|---|---|
| *(无)* | 监听 `127.0.0.1`，启用 UI 和审计日志 |
| `--share` | 监听 `0.0.0.0`，局域网内可访问 ⚠ |
| `--fast` | 禁用 UI 和审计，降低性能开销 |
| `--port N` | 指定端口（默认：8082） |

```bash
python cp4cc.py --share          # 局域网共享
python cp4cc.py --fast           # 纯代理模式，无 UI
python cp4cc.py --fast --share   # 组合使用
python cp4cc.py --port 9000      # 自定义端口
python cp4cc.py --help           # 查看完整帮助
```

### 配置 Claude Code

**方式一：持久化环境变量**
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8082
export ANTHROPIC_AUTH_TOKEN=dummy
claude
```

**方式二：单次启动**
```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8082 ANTHROPIC_AUTH_TOKEN=dummy claude
```

> 使用 `--share` 或 `--port` 时，请相应修改地址和端口。

## Dashboard

访问 **http://localhost:8082/ui** 打开可视化 Dashboard，自适应浏览器宽度，左右两栏布局：

**左栏 — Models**
- 列出所有可用的 Copilot 模型及其支持的接口
- Claude 模型（直接透传 `/v1/messages`）与其他模型分组展示

**右栏 — Requests（当前会话）**
- 实时请求列表，展示时间、模型、状态码、耗时、消息类型和内容预览
- 消息类型色标：`msg`（蓝）· `tool↑` tool_use（橙）· `tool↓` tool_result（紫）
- 工具类消息的预览列展示完整 body（工具名 + 入参 / 返回内容）
- 每 5 秒自动刷新，仅展示当前服务运行期间的请求

顶部配置栏提供 Claude Code 配置命令一键复制，以及实时请求统计（总数 / 成功 / 失败）。

## API 接口

| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /v1/models` | 获取可用模型列表（Anthropic 格式） |
| `POST /v1/messages` | 主代理接口（Anthropic 格式） |
| `POST /v1/responses` | 面向 Codex 的 OpenAI Responses API 接口 |
| `GET /v1/models/refresh` | 强制刷新模型列表 |
| `GET /audit/sessions` | 列出所有审计 session 文件 |
| `GET /audit/current` | 当前 session 审计日志（JSON） |
| `GET /ui` | 可视化 Dashboard |

> `--fast` 模式下，`/audit/*` 和 `/ui` 接口不可用。

## 模型名称映射

Claude Code 的模型名称会自动转换为 Copilot 格式：

| Claude Code | Copilot |
|---|---|
| `claude-opus-4-6` | `claude-opus-4.6` |
| `claude-opus-4-6-20250514` | `claude-opus-4.6` |
| `claude-haiku-4-5` | `claude-haiku-4.5` |
| `gpt-4o` | `gpt-4o`（不变） |

## 日志与审计

- 应用日志：`logs/app.log`
- 每次会话的审计日志：`logs/audit/session_YYYYMMDD_HHMMSS_<id>.json`

每条审计记录包含：原始模型名、映射后的 Copilot 模型名、接口路径、是否流式、逐条消息详情（role / 类型 / 完整内容）、状态码、耗时，以及响应体（流式响应最多保存 2000 字符）。

## 项目结构

```
.
├── cp4cc.py             # 代理服务（FastAPI）
├── requirements.txt     # Python 依赖
├── logs/
│   ├── app.log          # 应用日志
│   └── audit/           # 每次会话的审计 JSON 文件
└── .github_copilot_token/
    ├── access-token     # GitHub OAuth access token
    └── api-key.json     # Copilot API key（自动刷新）
```
