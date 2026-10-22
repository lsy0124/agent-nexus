"""Allowlisted, non-overwriting source export. Never uploads or copies Git history."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
TOP = {'README.md','README.zh-CN.md','LICENSE','CONTRIBUTING.md','SECURITY.md','CHANGELOG.md',
       'THIRD_PARTY_NOTICES.md','AGENTS.md','pytest.ini','.gitignore','.env.example'}
FRONT = {'index.html','package.json','package-lock.json','tsconfig.json','vite.config.ts'}
# Developer-only live scripts remain explicit opt-in; no runtime state is exported.
SCRIPT_EXT = {'.py','.ps1'}
SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
SYNTHETIC = re.compile(r'(?i)(fixture|synthetic|not.a.real|not.a.credential|test|example|dummy)')
BANNED = {'.git','.venv','node_modules','work','outputs','__pycache__','.pytest_cache','secrets'}


def allowed(name: str) -> bool:
    p = Path(name)
    if any(part in BANNED for part in p.parts):
        return False
    if name in TOP:
        return True
    parts = p.parts
    if len(parts) < 2:
        return False
    if parts[0] == 'backend':
        return (len(parts) == 2 and p.name in {'requirements.txt','requirements-dev.txt','requirements-lock.txt'}) or (
            len(parts) >= 3 and parts[1] in {'nexus','tests'} and p.suffix == '.py')
    if parts[0] == 'frontend':
        return (len(parts) == 2 and p.name in FRONT) or (
            len(parts) >= 3 and parts[1] == 'src' and p.suffix in {'.ts','.tsx','.css','.svg'}) or (
            len(parts) == 3 and parts[1] == 'tests' and p.name in {
                'e2e.mjs','run-e2e.mjs','codex-live.mjs','codex-evidence.mjs','visual-usage.mjs'})
    if parts[0] == 'scripts':
        return len(parts) == 2 and p.suffix in SCRIPT_EXT
    if parts[0] == 'integrations':
        return len(parts) == 3 and parts[1] in {'codex-cli','gemini-cli'} and p.name in {'package.json','package-lock.json'}
    if parts[0] == 'docs':
        return (len(parts) == 2 and (p.suffix == '.md' or p.name == 'DEPENDENCIES.json')) or (
            len(parts) == 3 and parts[1] == 'images' and p.name in {'console-desktop.png','console-mobile.png'})
    return len(parts) == 3 and parts[:2] == ('.github','workflows') and p.suffix in {'.yml','.yaml'}


def source_files(root: Path):
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in BANNED and not (Path(directory)/d).is_symlink()
                         and not (getattr((Path(directory)/d).lstat(),'st_file_attributes',0) & 0x400))
        for name in sorted(names):
            path = Path(directory)/name
            rel = path.relative_to(root).as_posix()
            if not allowed(rel):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink > 1 or getattr(info,'st_file_attributes',0) & 0x400:
                raise ValueError('Non-regular or linked source file: '+rel)
            if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Source redirect: '+rel)
            yield rel,path


def audit(root: Path):
    files, findings, fixtures = {}, [], []
    for rel,path in source_files(root):
        raw = path.read_bytes()
        files[rel] = {'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        if path.suffix == '.png':
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                findings.append({'path':rel,'rule':'invalid-png'})
            continue
        text = raw.decode('utf-8-sig')
        for i,line in enumerate(text.splitlines(),1):
            for match in SECRET.finditer(line):
                item = {'path':rel,'line':i,'rule':'credential-shaped-string'}
                # Explicit fixtures only in automated tests. Never exempt production config.
                if ('tests/' in rel) and SYNTHETIC.search(match.group()):
                    fixtures.append(item)
                else:
                    findings.append(item)
        if re.search(r'(?i)[A-Z]:[\\/]Users[\\/][^\\/\s\"\']+',text):
            findings.append({'path':rel,'rule':'personal-user-path-review'})
    return {'schema':1,'scope':'allowlisted source, not entire development history','files':files,
            'findings':findings,'synthetic_test_matches':fixtures,'passed':not findings,
            'limitations':['heuristic secret scan','binary screenshots need visual review','dependency advisories checked separately',
                           'operator state and Git history intentionally excluded; do not publish original checkout']}


def export(root: Path, destination: Path):
    result = audit(root)
    if not result['passed']:
        raise ValueError('Source audit failed; review paths/rules before export')
    parent = (root/'work/release-candidates').resolve()
    destination = destination.absolute()
    if not destination.resolve().is_relative_to(parent) or destination.resolve() == parent or destination.exists():
        raise ValueError('Export requires a new directory below work/release-candidates/')
    destination.mkdir(parents=True, exist_ok=False)
    for rel,metadata in result['files'].items():
        content = (root/rel).read_bytes()
        if hashlib.sha256(content).hexdigest() != metadata['sha256']:
            raise ValueError('Source changed during export: '+rel)
        target = destination/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:
            stream.write(content)
    (destination/'SOURCE_MANIFEST.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return destination,result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['audit','export'])
    args = parser.parse_args()
    result = audit(ROOT)
    if args.command == 'audit':
        print(json.dumps({k:v for k,v in result.items() if k != 'files'} | {'source_file_count':len(result['files'])},ensure_ascii=False,indent=2))
        return 0 if result['passed'] else 1
    destination = ROOT/'work/release-candidates'/('agent-nexus-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    dest,result = export(ROOT,destination)
    # ASCII JSON remains round-trippable through Windows' legacy console codecs.
    print(json.dumps({'candidate':str(dest),'source_files':len(result['files']),'audit_passed':result['passed'],'uploaded':False},ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
