# Security reporting

For a suspected vulnerability, use GitHub's private vulnerability reporting entry on this repository when available. Do not post exploit material containing real credentials or private data in a public issue. A redacted issue asking for a private reporting route is sufficient if the private entry is unavailable.

This is a local desktop automation server. It inherits the Windows user's rights and can perform consequential input. The default exclusion list removes general PowerShell, registry, filesystem, process, and web-scraping tools from MCP discovery; it is not a sandbox for the remaining desktop actions. Keep the AI client's authorization and approval controls in place.

The supported public launcher is stdio-only. Generic upstream HTTP/SSE serving modes and unreviewed dependency upgrades are outside this release's validated configuration. See PRIVACY.md and the limitations in the README.
