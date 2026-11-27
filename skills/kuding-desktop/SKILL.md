---
name: kuding-desktop
description: Use Kuding Desktop MCP to inspect and operate Windows desktop applications through scoped UI Automation, local OCR, screenshots, native actions, and verification.
---

# Kuding Desktop MCP

Use the MCP tools exposed by the installed `kuding-desktop` server. Read the user's instructions and the client's own action/confirmation policy first; this skill does not override either.

## Operating loop

1. Identify the intended application and window with `Window(mode="list")`. Restore or bring it forward if needed. A minimized or locked desktop is not a usable screenshot target.
2. Inspect a scoped target. Prefer `Find(selector="process:Notepad|role:Edit")`; use `Snapshot(foreground_only=true)` for a UI tree or `Screenshot(hwnd=...)` for a window image.
3. Prefer `SetValue` for editable native controls and `Invoke` for supported native actions. Use `FindText` / `ReadText` when the application does not expose a useful accessibility tree. Check the returned tool schema instead of guessing argument names.
4. For coordinates measured from a screenshot, respect its scale and screen offset. Pass the returned `frame` to supported coordinate actions. A frame guard checks window ownership; it does not prove that content inside the window stayed unchanged.
5. Check the result after acting. `ClickAndWatch` or a scoped `WaitUntil` can help observe changes. A dispatched click or a changed image is not proof that a business task completed.
6. Before typing, verify the window and focused control. Before submitting, deleting, or sending, verify the intended target and follow the user's authorization and client's confirmation rules.

## OCR and windows

- OCR can misread characters, return empty output, or match text in a background window. Scope to a known window/region and inspect ambiguous matches.
- A window can be readable while covered but not safely clickable. Bring it forward and refresh the screenshot before coordinate input.
- Menus, confirmation dialogs, and editor panels may be separate top-level windows. Enumerate them rather than repeatedly clicking a stale location.
- If human input or another MCP instance owns the desktop, yield. Do not disable the input lock merely to finish a task faster.
- Login credentials, verification codes, and system security prompts remain under the user's control. Never use these tools to evade the AI client's permissions or approval rules.

## Local diagnostics and feedback

The public build contains no instructions to upload logs. Review and redact local logs before voluntarily attaching them to a GitHub issue. Include the application version, expected/actual behavior, reproduction steps, and relevant tool output. Do not include customer records, account identifiers, credentials, or unredacted screenshots.

The server runs locally over stdio. The AI client may send returned screenshots/text to its model provider under that client's own settings. Local OCR and removal of upstream telemetry do not change the client's data handling.
