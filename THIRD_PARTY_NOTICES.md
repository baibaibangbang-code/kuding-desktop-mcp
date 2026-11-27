# Third-party notices

Kuding Desktop MCP contains modified files from CursorTouch/Windows-MCP 0.8.5. Its original MIT copyright and license are preserved in `pkg/LICENSE-windows-mcp.txt` and the root `LICENSE`.

The installer obtains the following pinned dependencies from PyPI; their source code and wheels are not bundled in this repository or its source release. Each distribution retains its own license. The links below lead to the corresponding distribution metadata. This inventory records package metadata, not a relicensing of those dependencies.

Upstream windows-mcp declares a PostHog dependency. This derivative intentionally omits that dependency, installs the explicit snapshot with `--no-deps`, and replaces the analytics sender before startup. Generic `pip install windows-mcp` or dependency repair can undo these changes; rerun our installer instead.

| Distribution | Version | Declared license |
|---|---|---|
| [Authlib](https://pypi.org/project/Authlib/1.8.0/) | 1.8.0 | BSD-3-Clause |
| [Levenshtein](https://pypi.org/project/Levenshtein/0.27.5/) | 0.27.5 | GPL-2.0-or-later |
| [PyJWT](https://pypi.org/project/PyJWT/2.14.0/) | 2.14.0 | MIT |
| [PyYAML](https://pypi.org/project/PyYAML/6.0.3/) | 6.0.3 | MIT |
| [Pygments](https://pypi.org/project/Pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| [RapidFuzz](https://pypi.org/project/RapidFuzz/3.14.6/) | 3.14.6 | MIT |
| [aiofile](https://pypi.org/project/aiofile/3.12.3/) | 3.12.3 | Apache-2.0 |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | MIT |
| [attrs](https://pypi.org/project/attrs/26.1.0/) | 26.1.0 | MIT |
| [backoff](https://pypi.org/project/backoff/2.2.1/) | 2.2.1 | MIT |
| [beartype](https://pypi.org/project/beartype/0.22.9/) | 0.22.9 | See distribution license files |
| [beautifulsoup4](https://pypi.org/project/beautifulsoup4/4.15.0/) | 4.15.0 | MIT License |
| [cachetools](https://pypi.org/project/cachetools/7.1.8/) | 7.1.8 | MIT |
| [caio](https://pypi.org/project/caio/0.12.4/) | 0.12.4 | Apache-2.0 |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | 2026.7.22 | MPL-2.0 |
| [cffi](https://pypi.org/project/cffi/2.1.1/) | 2.1.1 | MIT-0 |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/3.5.1/) | 3.5.1 | MIT |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | BSD-3-Clause |
| [comtypes](https://pypi.org/project/comtypes/1.4.16/) | 1.4.16 | MIT |
| [cryptography](https://pypi.org/project/cryptography/50.0.1/) | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| [cyclopts](https://pypi.org/project/cyclopts/4.25.2/) | 4.25.2 | Apache-2.0 |
| [distro](https://pypi.org/project/distro/1.9.0/) | 1.9.0 | Apache License, Version 2.0 |
| [dnspython](https://pypi.org/project/dnspython/2.8.0/) | 2.8.0 | ISC |
| [docstring_parser](https://pypi.org/project/docstring_parser/0.18.0/) | 0.18.0 | MIT |
| [dxcam](https://pypi.org/project/dxcam/0.3.0/) | 0.3.0 | MIT |
| [email-validator](https://pypi.org/project/email-validator/2.3.0/) | 2.3.0 | Unlicense |
| [exceptiongroup](https://pypi.org/project/exceptiongroup/1.3.1/) | 1.3.1 | See distribution metadata |
| [fastmcp](https://pypi.org/project/fastmcp/4.0.3/) | 4.0.3 | Apache-2.0 |
| [fastmcp-slim](https://pypi.org/project/fastmcp-slim/4.0.3/) | 4.0.3 | Apache-2.0 |
| [fuzzywuzzy](https://pypi.org/project/fuzzywuzzy/0.18.0/) | 0.18.0 | GPLv2 |
| [griffelib](https://pypi.org/project/griffelib/2.3.0/) | 2.3.0 | ISC |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [httpcore2](https://pypi.org/project/httpcore2/2.12.0/) | 2.12.0 | BSD-3-Clause |
| [httpx2](https://pypi.org/project/httpx2/2.12.0/) | 2.12.0 | BSD-3-Clause |
| [idna](https://pypi.org/project/idna/3.19/) | 3.19 | BSD-3-Clause |
| [jaraco.classes](https://pypi.org/project/jaraco.classes/3.4.0/) | 3.4.0 | See distribution metadata |
| [jaraco.context](https://pypi.org/project/jaraco.context/6.1.2/) | 6.1.2 | MIT |
| [jaraco.functools](https://pypi.org/project/jaraco.functools/4.6.0/) | 4.6.0 | MIT |
| [joserfc](https://pypi.org/project/joserfc/1.7.5/) | 1.7.5 | BSD-3-Clause |
| [jsonref](https://pypi.org/project/jsonref/1.1.0/) | 1.1.0 | MIT |
| [jsonschema](https://pypi.org/project/jsonschema/4.26.0/) | 4.26.0 | MIT |
| [jsonschema-path](https://pypi.org/project/jsonschema-path/0.5.0/) | 0.5.0 | Apache-2.0 |
| [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/2025.9.1/) | 2025.9.1 | MIT |
| [keyring](https://pypi.org/project/keyring/25.7.0/) | 25.7.0 | MIT |
| [markdown-it-py](https://pypi.org/project/markdown-it-py/4.2.0/) | 4.2.0 | See distribution metadata |
| [markdownify](https://pypi.org/project/markdownify/1.2.3/) | 1.2.3 | See distribution metadata |
| [mcp](https://pypi.org/project/mcp/2.2.0/) | 2.2.0 | MIT |
| [mcp-types](https://pypi.org/project/mcp-types/2.2.0/) | 2.2.0 | MIT |
| [mdurl](https://pypi.org/project/mdurl/0.1.2/) | 0.1.2 | See distribution metadata |
| [more-itertools](https://pypi.org/project/more-itertools/11.1.0/) | 11.1.0 | MIT |
| [numpy](https://pypi.org/project/numpy/2.5.3/) | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| [openapi-pydantic](https://pypi.org/project/openapi-pydantic/0.5.1/) | 0.5.1 | MIT |
| [opentelemetry-api](https://pypi.org/project/opentelemetry-api/1.44.0/) | 1.44.0 | Apache-2.0 |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pathable](https://pypi.org/project/pathable/0.6.0/) | 0.6.0 | Apache-2.0 |
| [pillow](https://pypi.org/project/pillow/12.3.0/) | 12.3.0 | MIT-CMU |
| [platformdirs](https://pypi.org/project/platformdirs/4.11.8/) | 4.11.8 | MIT |
| [psutil](https://pypi.org/project/psutil/7.2.2/) | 7.2.2 | BSD-3-Clause |
| [py-key-value-aio](https://pypi.org/project/py-key-value-aio/0.4.5/) | 0.4.5 | Apache-2.0 |
| [pycparser](https://pypi.org/project/pycparser/3.0/) | 3.0 | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/2.13.5/) | 2.13.5 | MIT |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/2.15.0/) | 2.15.0 | MIT |
| [pydantic_core](https://pypi.org/project/pydantic_core/2.46.5/) | 2.46.5 | MIT |
| [pyperclip](https://pypi.org/project/pyperclip/1.11.0/) | 1.11.0 | BSD |
| [python-Levenshtein](https://pypi.org/project/python-Levenshtein/0.27.5/) | 0.27.5 | GPL-2.0-or-later |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.3/) | 1.2.3 | BSD-3-Clause |
| [python-multipart](https://pypi.org/project/python-multipart/0.0.32/) | 0.0.32 | Apache-2.0 |
| [pywin32](https://pypi.org/project/pywin32/312/) | 312 | PSF |
| [pywin32-ctypes](https://pypi.org/project/pywin32-ctypes/0.2.3/) | 0.2.3 | BSD-3-Clause |
| [referencing](https://pypi.org/project/referencing/0.37.0/) | 0.37.0 | MIT |
| [requests](https://pypi.org/project/requests/2.34.2/) | 2.34.2 | Apache-2.0 |
| [rich](https://pypi.org/project/rich/15.0.0/) | 15.0.0 | MIT |
| [rich-rst](https://pypi.org/project/rich-rst/2.1.0/) | 2.1.0 | MIT |
| [rpds-py](https://pypi.org/project/rpds-py/2026.6.3/) | 2026.6.3 | MIT |
| [six](https://pypi.org/project/six/1.17.0/) | 1.17.0 | MIT |
| [soupsieve](https://pypi.org/project/soupsieve/2.9.2/) | 2.9.2 | MIT |
| [sse-starlette](https://pypi.org/project/sse-starlette/3.4.11/) | 3.4.11 | BSD-3-Clause |
| [starlette](https://pypi.org/project/starlette/1.6.0/) | 1.6.0 | BSD-3-Clause |
| [tabulate](https://pypi.org/project/tabulate/0.10.0/) | 0.10.0 | MIT |
| [thefuzz](https://pypi.org/project/thefuzz/0.22.1/) | 0.22.1 | MIT |
| [truststore](https://pypi.org/project/truststore/0.10.4/) | 0.10.4 | MIT |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [typing_extensions](https://pypi.org/project/typing_extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [uncalled-for](https://pypi.org/project/uncalled-for/0.4.0/) | 0.4.0 | See distribution license files |
| [urllib3](https://pypi.org/project/urllib3/2.7.0/) | 2.7.0 | MIT |
| [uuid7](https://pypi.org/project/uuid7/0.1.0/) | 0.1.0 | MIT license |
| [uvicorn](https://pypi.org/project/uvicorn/0.52.4/) | 0.52.4 | BSD-3-Clause |
| [watchfiles](https://pypi.org/project/watchfiles/1.2.0/) | 1.2.0 | MIT |
| [websockets](https://pypi.org/project/websockets/17.1/) | 17.1 | BSD-3-Clause |
| [windows-mcp](https://pypi.org/project/windows-mcp/0.8.5/) | 0.8.5 | See distribution license files |
| [winrt-Windows.Foundation](https://pypi.org/project/winrt-Windows.Foundation/3.2.1/) | 3.2.1 | MIT |
| [winrt-Windows.Foundation.Collections](https://pypi.org/project/winrt-Windows.Foundation.Collections/3.2.1/) | 3.2.1 | MIT |
| [winrt-Windows.Globalization](https://pypi.org/project/winrt-Windows.Globalization/3.2.1/) | 3.2.1 | MIT |
| [winrt-Windows.Graphics.Imaging](https://pypi.org/project/winrt-Windows.Graphics.Imaging/3.2.1/) | 3.2.1 | MIT |
| [winrt-Windows.Media.Ocr](https://pypi.org/project/winrt-Windows.Media.Ocr/3.2.1/) | 3.2.1 | MIT |
| [winrt-Windows.Storage.Streams](https://pypi.org/project/winrt-Windows.Storage.Streams/3.2.1/) | 3.2.1 | MIT |
| [winrt-runtime](https://pypi.org/project/winrt-runtime/3.2.1/) | 3.2.1 | MIT |
