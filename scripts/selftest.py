"""Check stdio negotiation and tool schemas without observing or acting on UI."""
import argparse
import importlib.util
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    serve = root / 'serve.py'
    if not serve.exists():
        raise SystemExit('Run the installed copy of selftest.py.')
    if importlib.util.find_spec('posthog') is not None:
        raise SystemExit('FAIL: PostHog SDK must not be installed in this runtime.')
    p = subprocess.Popen([sys.executable, str(serve)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    messages = queue.Queue()
    errors = []
    def read_stdout():
        for line in p.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                pass
    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=lambda: errors.extend(p.stderr), daemon=True).start()
    def send(obj):
        p.stdin.write(json.dumps(obj) + '\n')
        p.stdin.flush()
    def wait_for(i):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                msg = messages.get(timeout=.2)
            except queue.Empty:
                if p.poll() is not None:
                    break
                continue
            if msg.get('id') == i:
                if 'error' in msg:
                    raise RuntimeError(str(msg['error']))
                return msg['result']
        raise RuntimeError('MCP response missing. ' + ''.join(errors[-12:]))
    try:
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'kuding-selftest', 'version': '0.6.0'}}})
        wait_for(1)
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        tools = wait_for(2)['tools']
        names = {t['name'] for t in tools}
        required = {'Snapshot', 'Screenshot', 'Find', 'Click', 'SetValue', 'Invoke', 'Window', 'FindText', 'ReadText', 'ClickAndWatch'}
        missing = sorted(required - names)
        if missing:
            raise RuntimeError('Missing tools: ' + ', '.join(missing))
        assert all(isinstance(t.get('inputSchema'), dict) for t in tools)
        print(json.dumps({'ok': True, 'tools': len(tools), 'missing': missing, 'posthog_installed': False, 'ui_actions_performed': 0, 'tool_names': sorted(names)}, ensure_ascii=False))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


if __name__ == '__main__':
    main()
