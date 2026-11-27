# Validation scope

## Historical development evidence

The internal code line was used during development with Power BI, a monitoring/export client, and custom-drawn chat interfaces, through several MCP-capable AI clients. Those reports motivated the overlay changes. They are historical evidence, not a fresh cross-version compatibility benchmark.

Business documents, account data, raw screenshots, and internal reports are deliberately not published. No performance or universal compatibility claim is inferred from the conceptual illustration in the examples.

## Public release verification

The public release is checked separately for source sanitation, overlay integrity, clean isolated installation, local-only analytics behavior, and MCP initialization/tool schemas. The release notes record the results actually observed for the tagged snapshot.

Observed on 2026-11-27, Windows 11 x64 (10.0.26200), CPython 3.13.14:

- Fresh installation into a separate, initially absent runtime directory; 98 pinned, hashed distributions installed with the telemetry SDK omitted.
- 25 overlay file hashes verified; MCP initialization and tool discovery succeeded with 30 default tools and no required tools missing.
- The local analytics regression test passed: no socket connection during the exercised calls, local identity/client behavior, synchronous-function thread scheduling, and original exception propagation.
- An explicit desktop test used the project's synthetic WinForms fixture through the newly installed MCP: window discovery, scoped control lookup, native value setting/readback, button invocation, fixture result verification, and a fixture-only screenshot all succeeded. The test closed its own window afterward. See [the demo evidence](demo-evidence.json) and [reviewed image](assets/demo.png).

The protocol self-test performs zero desktop observations/actions. The separate desktop fixture test is opt-in and is not run by the installer or CI.

The installation self-test does not observe or act on application windows. Full client-by-client UI workflows, every Windows edition, Windows 10, ARM64, and every OCR language are not covered by that self-test. User-submitted reproductions should state their actual environment and the exact step that succeeded or failed.
