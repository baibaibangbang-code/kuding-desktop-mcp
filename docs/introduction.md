# Kuding Desktop MCP: Windows desktop automation shaped by real tasks

**English** | [简体中文](introduction.zh-CN.md)

Opening an application, finding a button, entering text, and checking the result are becoming practical parts of working with AI assistants. Kuding Desktop MCP began with concrete needs in our own work.

## Why develop it when computer-use tools already existed?

When the project started, ChatGPT already offered computer-control tools, and we were using them. At the same time, our WorkBuddy setup lacked a dedicated plugin or MCP for controlling the local desktop. We wanted other MCP-capable AI clients to help operate Windows applications and handle steps that otherwise required manual clicking, typing, and checking.

As we used existing tools, we encountered limitations in application compatibility, available tool capabilities, and interaction methods. We wanted a desktop automation layer whose code we could inspect, whose behavior we could adapt, and whose application-specific lessons we could retain.

Kuding Desktop MCP complements ChatGPT's first-party computer-use capabilities and provides another option for MCP-capable clients. Its use remains subject to the user's authorization and the AI client's own permissions and approval rules.

## Building on open source

Kuding Desktop MCP is based on [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) 0.8.5. The upstream project provided an important foundation. We preserve its original copyright notice and MIT license, identify that foundation, and describe our modifications separately.

During development, we used the tool to work with Power BI dashboards, export information through a monitoring application's interface, and operate chat applications with custom-drawn interfaces. These tasks led to specific improvements:

- Full-desktop searches could be slow or miss controls, so we added process- and window-scoped lookup.
- Custom-drawn interfaces did not always expose useful accessibility controls, so we combined local OCR with screenshots.
- Covered or moved windows could invalidate old coordinates, so we added screenshot/window ownership checks.
- Dispatching a click did not prove that a task had completed, so we added observation, condition waiting, and clearer result descriptions.
- Multiple clients and people could compete for the same desktop input, so we added coordination between compatible instances and yielding to human input.

We try to connect each improvement to a reproducible problem and record the cause, response, and verification scope. These examples summarize the development history; the public release's current checks are documented separately in [validation.md](validation.md).

## Removing upstream automatic telemetry

The upstream version we adopted included PostHog usage statistics and error reporting. Such features can help developers understand usage and improve their tools. Because desktop tasks can involve internal files and business information, we chose to remove that automatic telemetry sender.

The public build replaces the sending module with local diagnostics, preserves the asynchronous scheduling interface required by the tools, omits the PostHog SDK, and checks that the replacement is present before starting. A compatibility name such as `PostHogAnalytics` may remain in the code, but the replacement creates no PostHog client and does not generate the original persistent anonymous identifier.

The tool does not automatically send tool-call or error information to the upstream statistics service through that PostHog mechanism. The public package also omits the internal testing log-upload instructions and distribution service. Users choose whether to submit feedback.

Local logs can help diagnose problems, but error messages may contain window titles or file paths and should be redacted before sharing. An AI client may send screenshots or interface text to its model provider. Installation/update downloads and explicitly enabled network tools also have network behavior. These boundaries are explained separately in [PRIVACY.md](../PRIVACY.md).

## Connecting observation, action, and verification

For observation, the tool first uses control information exposed through Windows UI Automation, supplementing incomplete accessibility information with screenshots and OCR.

For interaction, it can locate targets by control name, type, AutomationId, window, or process. Controls supporting native UIA actions can receive value changes, selections, expansion, and other operations directly.

For verification, the tool offers change observation and condition waiting to help the AI check the resulting interface state. An action being dispatched, an image changing, and the business task being completed each require their own evidence.

The accompanying skill records general operating methods and common problems to help the AI choose appropriate tools. The code and skill together retain lessons from actual use.

## Intended users and the open-source goal

The project is intended for individuals, developers, and small teams who want AI assistance with Windows applications, particularly workflows involving several applications, many interface steps, or no suitable API.

Results depend on the application's control implementation, desktop state, Windows permissions, display scaling, and OCR support. We document verified scenarios and known limitations, and welcome reproducible cases that help improve the tool.

Our goal is to help AI assistants see the Windows desktop more clearly, explain failures more precisely, and provide evidence when reporting that a task is complete.
