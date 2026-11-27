# 安装、升级、自检与卸载

## 首次安装

1. 使用 Windows 10/11 x64；本轮验证为 Windows 11 x64。登录并解锁桌面。
2. 按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv，重新打开 PowerShell，确认 `uv --version` 可用。
3. 克隆本仓库，或解压 GitHub Release 的源码包。进入含 `install.ps1` 的项目目录。
4. 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`。此参数只用于这次脚本进程，不修改系统执行策略。
5. 安装器创建独立 Python 3.13 环境，安装带哈希的固定依赖，应用 25 个源码覆盖文件，然后进行 MCP 协议自检。
6. 将生成的 `mcp.json` 中的服务合并到 AI 客户端配置，重启该连接。

安装器需要联网，不下载模型、不要求模型 API key，也不自动修改客户端配置。uv 可能按需下载 Python。安装失败会返回非零退出状态；不要只根据目录存在就认为安装成功。

## 自定义目录与网络

```powershell
.\install.ps1 -Dir 'D:\Tools\KudingDesktopMCP'
.\install.ps1 -UvExecutable 'D:\Tools\uv.exe'
.\install.ps1 -IndexUrl 'https://pypi.org/simple'
```

默认使用 uv 的包索引和网络配置。需要代理时，可以在此次 PowerShell 会话中设置 uv 支持的网络环境变量。公开安装器不扫描代理端口、不写入系统代理设置。

安装目标必须是空目录或带本项目标记的已有安装。公开版默认目录与旧内部版分开；不要指向其他软件或生产环境目录。

## 自检

```powershell
& "$env:LOCALAPPDATA\KudingDesktopMCP\venv\Scripts\python.exe" "$env:LOCALAPPDATA\KudingDesktopMCP\selftest.py"
```

成功输出 JSON，包含 `ok: true`、`missing: []`、`posthog_installed: false`。工具数量会受排除项影响。自检仅初始化 MCP、读取工具定义，不调用桌面观察或输入工具。

## 工具范围

默认排除 `PowerShell,Registry,FileSystem,Process,Scrape`。这仍然是一个能启动应用和操作输入的本地 MCP，具体任务应由客户端遵循用户授权执行。

需要调整时，重新运行安装器，例如保留前四项但开放网页读取：

```powershell
.\install.ps1 -ExcludeTools 'PowerShell,Registry,FileSystem,Process'
```

也可以编辑安装目录中的 `tool-options.json`，其 `exclude_tools` 为工具名数组；修改后重启 MCP 连接。空数组开放全部工具。工具能力和风险应与使用者明确约定。

## 更新

下载或检出新的已审阅版本，停止当前 MCP 连接，用相同 `-Dir` 重新安装，然后重启连接。安装器会重新生成启动配置和排除项，请保留需要复用的参数。工具没有后台自动更新或自动日志上报服务。

不要用裸 `pip install --upgrade windows-mcp` 更新本项目。那会安装未经本项目验证的上游版本，可能覆盖隐私与功能补丁。启动器会拒绝缺失本地分析替换模块、或重新出现 PostHog SDK 的环境。

依赖快照使用显式 `--no-deps` 和哈希安装，是为了省略上游仍声明的 PostHog 依赖。通用依赖修复工具可能提示该项缺失，或尝试重新安装它；请使用本项目安装器维护此独立环境。后续升级依赖需要重新进行协议与隐私回归验证。

## 卸载

先从客户端移除 `kuding-desktop` 条目并停止该 MCP 连接。确认安装路径以及是否要保留本地日志、注册表备份后，仅删除该 Kuding 安装目录。源码目录可另行保留或删除。

本项目没有安装 Windows 服务、开机启动或定时任务，卸载时无需修改这些系统设置；也不应删除其他 MCP 的目录或全局 Windows-MCP 配置。
