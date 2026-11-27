"""Opt-in native fixture test. Acts only on its uniquely titled test window."""
import argparse
import base64
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-desktop-demo', action='store_true')
    args = parser.parse_args()
    if not args.run_desktop_demo:
        raise SystemExit('This test opens and operates a synthetic desktop window. Opt in explicitly.')
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    result_file = args.output / 'fixture-result.json'
    if result_file.exists():
        raise SystemExit('Choose a fresh output directory.')
    fixture = subprocess.Popen(['powershell.exe', '-NoProfile', '-STA', '-File', str(root / 'tests/fixtures/demo.ps1'), '-ResultPath', str(result_file)], creationflags=subprocess.CREATE_NO_WINDOW)
    server = subprocess.Popen([str(args.runtime / 'venv/Scripts/python.exe'), str(args.runtime / 'serve.py')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    received = queue.Queue()
    errors = []
    def reader():
        for line in server.stdout:
            try:
                received.put(json.loads(line))
            except json.JSONDecodeError:
                pass
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=lambda: errors.extend(server.stderr), daemon=True).start()
    counter = 0
    def call(method, params):
        nonlocal counter
        counter += 1
        server.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params}) + '\n')
        server.stdin.flush()
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            try:
                msg = received.get(timeout=.2)
            except queue.Empty:
                if server.poll() is not None:
                    break
                continue
            if msg.get('id') == counter:
                if 'error' in msg:
                    raise RuntimeError(str(msg['error']))
                return msg['result']
        raise RuntimeError('MCP timeout: ' + ''.join(errors[-8:]))
    def tool(name, arguments):
        result = call('tools/call', {'name': name, 'arguments': arguments})
        texts = '\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')
        if result.get('isError'):
            raise RuntimeError(name + ': ' + texts)
        return result, texts
    hwnd = None
    try:
        call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'kuding-fixture-test', 'version': '0.6.0'}})
        server.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        server.stdin.flush()
        tool('Window', {'mode': 'list', 'title': 'Kuding Desktop MCP Demo', 'detail': True})
        _, listed = tool('Window', {'mode': 'list', 'title': 'Kuding Desktop MCP Demo', 'detail': True})
        match = re.search(r'hwnd=(\d+)', listed)
        if not match:
            raise RuntimeError('Own fixture window was not found: ' + listed)
        hwnd = int(match[1])
        tool('Window', {'mode': 'front', 'hwnd': hwnd})
        selector = 'window:Kuding Desktop MCP Demo|role:Edit'
        _, observed = tool('Find', {'selector': selector, 'limit': 5})
        print('Observed fixture edit control: ' + observed, flush=True)
        _, set_result = tool('SetValue', {'selector': selector, 'value': 'Kuding Desktop MCP test'})
        print('SetValue: ' + set_result, flush=True)
        _, click_result = tool('Invoke', {'selector': 'window:Kuding Desktop MCP Demo|role:Button|name:Verify'})
        print('Invoke: ' + click_result, flush=True)
        deadline = time.monotonic() + 5
        while not result_file.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        verified = json.loads(result_file.read_text()) if result_file.exists() else {}
        if not verified.get('verified'):
            raise RuntimeError('The fixture did not confirm the expected input.')
        shot, _ = tool('Screenshot', {'hwnd': hwnd})
        images = [c for c in shot.get('content', []) if c.get('type') == 'image']
        if len(images) != 1:
            raise RuntimeError('Expected one screenshot of the fixture window.')
        (args.output / 'demo.png').write_bytes(base64.b64decode(images[0]['data']))
        evidence = {'fixture': 'Synthetic WinForms window', 'native_write_verified': True, 'native_invoke_verified': True, 'fixture_result_verified': True, 'screenshot_scope': 'fixture window only', 'business_data_used': False}
        (args.output / 'evidence.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(evidence), flush=True)
    finally:
        if hwnd and server.poll() is None:
            try:
                tool('Window', {'mode': 'close', 'hwnd': hwnd})
            except Exception:
                pass
        if fixture.poll() is None:
            fixture.terminate()
        fixture.wait(timeout=10)
        server.terminate()
        server.wait(timeout=10)


if __name__ == '__main__':
    main()
