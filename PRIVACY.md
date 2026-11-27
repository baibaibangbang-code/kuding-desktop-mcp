# Privacy and data boundaries / 隐私边界

## Removed upstream telemetry

This derivative targets Windows-MCP 0.8.5. Its PostHog sender is replaced by a local-only compatibility module before MCP starts. The public dependency snapshot omits the PostHog SDK; the launcher refuses to start if that SDK reappears or the expected overlay is missing. It sets `ANONYMIZED_TELEMETRY=false` and disables inherited OpenTelemetry SDK/exporter settings for this MCP process.

The compatibility class name is retained because other upstream modules import it. It creates no telemetry client and returns a nonpersistent local identity. Local diagnostic output includes tool status and errors; error text may contain private window titles or file paths.

## Public package behavior

- No internal machine keys, distribution endpoints, operator log-upload instructions, or log-upload service are shipped.
- Installation/update downloads pinned dependencies and may download Python through uv. There is no background updater.
- The launcher selects local stdio transport and its own config file. It does not expose an HTTP/SSE listener.
- Screenshots and OCR are returned to the requesting AI client. The client may transmit them to its model provider; this project cannot change that client's data handling.
- Explicitly enabled tools, application launches, or user-approved commands can have network effects. Removing telemetry does not create a network sandbox.
- Optional local logs and backups stay in the chosen installation directory or paths you explicitly configure. Review them before sharing.

## 中文说明

本项目移除的是采用的上游版本中 PostHog 的自动使用统计和错误报送，并非承诺整台电脑、所有客户端或所有工具都不联网。

公开版不携带内部分发站、机器标识或运行日志回传流程。反馈由使用者主动提交。AI 客户端对截图与文字的处理、安装更新时的下载、显式启用的联网工具行为，均与上游遥测分别看待。

本地诊断有助于排错，也可能含窗口标题、文件路径和错误中的业务文字。向 GitHub 提交材料之前，请检查并脱敏；无需提供模型 key、系统凭据或私有业务文件。
