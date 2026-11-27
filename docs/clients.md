# MCP client configuration / 客户端配置

The installer writes your actual absolute paths to `<installation>/mcp.json`. Merge only its server entry; preserve unrelated configuration. Restart the MCP connection after changing it. Client authentication, model settings, tool approval, and organization policy remain with the client.

安装器生成真实路径的配置。下面的 `<YOUR_USER>` 仅为示例，必须替换；JSON/TOML 不会自动展开 `%LOCALAPPDATA%`。工具以本地 stdio 连接，不需要监听 HTTP 端口。

## Claude Code and JSON-style clients

```json
{
  "mcpServers": {
    "kuding-desktop": {
      "command": "C:\\Users\\<YOUR_USER>\\AppData\\Local\\KudingDesktopMCP\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\<YOUR_USER>\\AppData\\Local\\KudingDesktopMCP\\serve.py"]
    }
  }
}
```

Claude Code 可将该条目合并到当前用户或项目的 MCP 配置。使用命令行添加时：

```powershell
claude mcp add --transport stdio --scope user kuding-desktop -- "$env:LOCALAPPDATA\KudingDesktopMCP\venv\Scripts\python.exe" "$env:LOCALAPPDATA\KudingDesktopMCP\serve.py"
claude mcp list
```

WorkBuddy 的本地 MCP 管理入口可按上述 command / args 建立 stdio 连接。历史版本使用过带 `type: "stdio"` 的 `mcpServers` 条目，具体字段和审批入口以当前客户端版本为准。本次发布没有改动使用者的 WorkBuddy 配置。

## Codex TOML

在 Codex 的用户配置 `~/.codex/config.toml` 中合并一个独立条目。TOML 单引号字符串保留 Windows 反斜杠：

```toml
[mcp_servers.kuding-desktop]
command = 'C:\Users\<YOUR_USER>\AppData\Local\KudingDesktopMCP\venv\Scripts\python.exe'
args = ['C:\Users\<YOUR_USER>\AppData\Local\KudingDesktopMCP\serve.py']
startup_timeout_sec = 60
tool_timeout_sec = 60
```

不要重复添加已有同名条目。MCP 提供工具能力，不改变 Codex 的授权和工具审批策略。

## OpenCode

OpenCode 使用自己的 local MCP 结构，例如：

```json
{
  "mcp": {
    "kuding-desktop": {
      "type": "local",
      "command": [
        "C:\\Users\\<YOUR_USER>\\AppData\\Local\\KudingDesktopMCP\\venv\\Scripts\\python.exe",
        "C:\\Users\\<YOUR_USER>\\AppData\\Local\\KudingDesktopMCP\\serve.py"
      ],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

公开版不包含旧内部版的自动下载插件，也不需要机器 key。使用标准本地 MCP 配置即可。不同客户端版本字段可能变化；若自检成功但客户端无法连接，请附脱敏的启动错误和客户端版本提交问题。
