# Contributing / 参与贡献

Start with a reproducible desktop problem. Explain the target application's version, what the user intended, what the tool actually observed/did, and the result. Redact private information before submitting an issue or PR.

1. Create a branch; keep a PR focused on one behavior.
2. Explain the concrete trigger and expected/actual result.
3. State the verification environment and scope. Mark synthetic fixtures and protocol-only tests accurately; do not claim a full application workflow passed unless you checked it.
4. Run `python scripts/check_public_tree.py`. On a disposable Windows installation run the installer and `python -m unittest discover -s tests -v` using its Python.
5. When changing overlay files, update `public-source-manifest.json` to the new SHA-256 values after reviewing the diff. A mismatch must never be silenced without checking the change.
6. Keep Windows PowerShell / batch scripts ASCII for compatibility with PowerShell 5.1. Keep all text reads/writes explicit about encoding.
7. Preserve upstream attribution and licenses. Do not submit credentials, logs, private paths, customer data, or unrelated screenshots.

公开仓库从脱敏快照开始维护。请附上真实复现步骤与验证范围；不要求回传任何业务日志。涉及依赖升级、安装维护和隐私路径的修改，需要同时验证固定依赖、MCP 发现和无上游遥测的行为。
