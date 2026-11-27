# Public snapshot review / 公开快照检查

Review date: 2026-11-27.

- Exported only the runtime overlay needed for this derivative. Internal deployment services, keys/configuration, operational handoff material, business screenshots, original development notes containing personal context, and the private Git history are not included.
- Generalized personal references in retained source comments/docstrings. The public commit identity uses the project's contributor name and a GitHub noreply address.
- Replaced the internal installer, updater, and log-upload instructions with a standalone installer and voluntary issue reporting. The public launcher starts only stdio with its own config.
- Preserved upstream attribution and MIT license; documented separately downloaded dependencies.
- Checked public text for credential patterns, private paths, internal identifiers and distribution endpoints. A separate detect-secrets scan was reviewed: its findings were the SHA-256 checksums in the source/media manifests, not access credentials.
- The only approved binary is a fixture-only screenshot. It was visually checked and its SHA-256 is recorded in approved-media.json. It contains no business documents, accounts or personal paths.
- Fresh isolated installation, overlay hashes, MCP initialization/tool discovery, local analytics regression, and a native synthetic desktop fixture were checked. See validation.md for exact scope.

This is a review of the published snapshot, not a guarantee about every future contribution, external AI client, third-party application, or dependency update. Keep reviews and validation current when changing those parts.
