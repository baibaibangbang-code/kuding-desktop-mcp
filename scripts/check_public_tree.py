"""Static publication checks. Findings report paths/lines, never secret values."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', 'venv', '__pycache__', 'build', 'dist'}
RULES = {
    'personal-home': r'C:[\\/]+Users[\\/]+(?!<|YOUR|example|Public|Default)[A-Za-z0-9_.-]+',
    'github-token': r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}',
    'provider-key': r'(?:sk-proj-|sk-ant-|sk_live_|AKIA)[A-Za-z0-9_-]{16,}',
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'internal-id': r'DjFg[A-Za-z0-9]{20,}|WO4G[A-Za-z0-9]{10,}',
    'private-service': r'uia-desktop\.vercel\.app|[a-z0-9]+\.public\.blob\.vercel-storage\.com',
}


def main():
    failures = []
    count = 0
    approved = json.loads((ROOT / 'docs/approved-media.json').read_text(encoding='utf-8'))
    for path in ROOT.rglob('*'):
        if not path.is_file() or any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        count += 1
        relative = path.relative_to(ROOT).as_posix()
        if relative in approved:
            if hashlib.sha256(path.read_bytes()).hexdigest() != approved[relative]['sha256']:
                failures.append(f'{relative}: reviewed media hash mismatch')
            continue
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.mp4', '.sqlite', '.db', '.pem', '.key'}:
            failures.append(f'{path.relative_to(ROOT)}: forbidden publication artifact')
            continue
        try:
            content = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            failures.append(f'{path.relative_to(ROOT)}: unexpected binary artifact')
            continue
        if path.suffix == '.py':
            ast.parse(content, filename=str(path))
        if path.suffix in {'.ps1', '.cmd'} and not content.isascii():
            failures.append(f'{path.relative_to(ROOT)}: shell script must be ASCII')
        for label, expression in RULES.items():
            for match in re.finditer(expression, content, re.IGNORECASE):
                line = content.count('\n', 0, match.start()) + 1
                failures.append(f'{path.relative_to(ROOT)}:{line}: {label}')
    manifest = json.loads((ROOT / 'public-source-manifest.json').read_text(encoding='utf-8'))
    for entry in manifest['patches']:
        if hashlib.sha256((ROOT / entry['path']).read_bytes()).hexdigest() != entry['sha256']:
            failures.append(f'{entry["path"]}: manifest hash mismatch')
    analytics = (ROOT / 'pkg/patch/windows_mcp/infrastructure/analytics.py').read_text(encoding='utf-8')
    tree = ast.parse(analytics)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(n.name.split('.')[0] in {'posthog', 'requests', 'httpx', 'httpx2', 'urllib', 'socket'} for n in node.names):
            failures.append('analytics.py: unexpected network import')
        if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in {'posthog', 'requests', 'httpx', 'httpx2', 'urllib', 'socket'}:
            failures.append('analytics.py: unexpected network import')
    if failures:
        print('\n'.join(failures))
        raise SystemExit(1)
    print(json.dumps({'ok': True, 'public_files_checked': count, 'patch_hashes_verified': len(manifest['patches'])}))


if __name__ == '__main__':
    main()
