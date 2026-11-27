"""Start only the locally patched stdio server, with an isolated config."""
import importlib.util
import json
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    os.environ['ANONYMIZED_TELEMETRY'] = 'false'
    os.environ['KUDING_DESKTOP_HOME'] = str(root)
    os.environ.setdefault('WINDOWS_MCP_MAX_TREE_ELEMENTS', '8000')
    os.environ.setdefault('UIA_DESKTOP_SCRAPE_LOG', str(root / 'scrape.log'))
    # Prevent a client-provided OpenTelemetry exporter from silently changing
    # this launcher's diagnostics contract. This applies only to this process.
    os.environ['OTEL_SDK_DISABLED'] = 'true'
    for key in tuple(os.environ):
        if key.startswith('OTEL_EXPORTER_'):
            del os.environ[key]
    if importlib.util.find_spec('posthog') is not None:
        raise SystemExit('PostHog is present. Rerun the Kuding installer before starting.')
    from windows_mcp.infrastructure import analytics
    text = Path(analytics.__file__).read_text(encoding='utf-8')
    if 'Local-only diagnostics for Kuding Desktop MCP.' not in text:
        raise SystemExit('The local-only analytics overlay is missing. Rerun the installer.')
    options = json.loads((root / 'tool-options.json').read_text(encoding='utf-8'))
    from windows_mcp.__main__ import main as upstream_main
    # An explicit config prevents inherited ~/.windows-mcp settings from
    # unexpectedly selecting a network transport or changing tool exclusions.
    conf = root / 'server.toml'
    conf.write_text('[server]\ntransport = "stdio"\n', encoding='utf-8')
    argv = ['serve', '--config', str(conf), '--transport', 'stdio']
    excluded = options.get('exclude_tools', [])
    if excluded:
        argv.extend(['--exclude-tools', ','.join(excluded)])
    upstream_main(args=argv, prog_name='kuding-desktop')


if __name__ == '__main__':
    main()
