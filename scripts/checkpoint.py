"""Non-destructive source checkpoint. Never traverses client data or vaults."""
from pathlib import Path
import datetime
import hashlib
import json
import sqlite3
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / 'work' / 'backups' / ('audit-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f'))
    target.mkdir(parents=True, exist_ok=False)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT)
    (target / 'status.txt').write_bytes(git('status', '--short'))
    (target / 'worktrees.txt').write_bytes(git('worktree', 'list', '--porcelain'))
    (target / 'tracked.patch').write_bytes(git('diff', 'HEAD', '--binary'))
    subprocess.run(['git', 'bundle', 'create', str(target / 'history.bundle'), '--all'], cwd=ROOT, check=True, capture_output=True)
    subprocess.run(['git', 'bundle', 'verify', str(target / 'history.bundle')], cwd=ROOT, check=True, capture_output=True)
    names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').decode('utf-8').split('\0')
    manifest = {}
    with zipfile.ZipFile(target / 'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(set(names)):
            if not name:
                continue
            path = ROOT / name
            if any(part in {'work', '.venv', 'node_modules', '.git', 'secrets', '.codex', '.agents', '.aws', '.ssh'} for part in Path(name).parts):
                continue
            if path.name.lower() in {'auth.json', 'oauth_creds.json', 'credentials.json', 'cookies.json', 'tokens.json'}:
                continue
            if path.name.startswith('.env') or path.suffix in {'.dpapi', '.sqlite3', '.sqlite'} or path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(ROOT.resolve()) or path.stat().st_nlink > 1:
                continue
            content = path.read_bytes()
            archive.writestr(name, content)
            manifest[name] = hashlib.sha256(content).hexdigest()
    # Snapshot task files separately; no traversal of .git pointers, secrets or caches.
    task_files = {}
    with zipfile.ZipFile(target / 'task-changes.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        trees = ROOT / 'work' / 'worktrees'
        for tree in sorted(trees.iterdir()) if trees.exists() else []:
            if tree.is_symlink() or not tree.is_dir():
                continue
            changed = subprocess.check_output(['git', 'ls-files', '-z', '--modified', '--others', '--exclude-standard'], cwd=tree).decode('utf-8').split('\0')
            for name in set(changed):
                path = tree / name
                if not name or not path.is_file() or path.is_symlink() or '.env' in path.name or path.suffix == '.dpapi':
                    continue
                if path.name.lower() in {'auth.json', 'oauth_creds.json', 'credentials.json', 'cookies.json', 'tokens.json'} or path.stat().st_nlink > 1:
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(tree.resolve()):
                    continue
                content = path.read_bytes()
                relative = tree.name + '/' + name
                archive.writestr(relative, content)
                task_files[relative] = hashlib.sha256(content).hexdigest()
    database = ROOT / 'work' / 'nexus.sqlite3'
    if database.exists():
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(target / 'nexus.sqlite3') as destination:
                source.backup(destination)
    (target / 'manifest.json').write_text(json.dumps({'source': manifest, 'task_changes': task_files}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checkpoint': str(target), 'source_files': len(manifest), 'task_files': len(task_files), 'bundle_verified': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
