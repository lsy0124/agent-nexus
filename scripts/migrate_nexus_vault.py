"""Explicit one-time migration of *Nexus's own* previously created DPAPI store.

Does not inspect any Agent login files. Plaintext is never printed/exported;
the destination is re-encrypted for the same Windows user inside ignored work/.
The source is left unchanged. Run only after the operator requests persistence.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from nexus import catalog, keyvault
from nexus.storage import Store, now


def main():
    if '--approve-own-nexus-store' not in sys.argv:
        raise SystemExit('Explicit --approve-own-nexus-store required; nothing read or changed')
    source = Path(os.environ['LOCALAPPDATA']) / 'AgentNexus' / 'secrets'
    destination = keyvault.SecretVault(ROOT, backend='dpapi', directory=ROOT / 'work' / 'secrets')
    if destination.backend != 'dpapi':
        raise RuntimeError('DPAPI unavailable; refusing plaintext fallback')
    if destination.load_errors:
        raise RuntimeError('Destination contains unreadable ciphertext; refusing overwrite')
    imported = []
    for provider_id in catalog.PROVIDERS:
        path = source / (provider_id + '.dpapi')
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_nlink > 1:
            raise RuntimeError('Refusing linked Nexus ciphertext')
        document = json.loads(keyvault._unprotect(path.read_bytes()).decode('utf-8'))
        if document.get('provider_id') != provider_id or document.get('schema') != 1:
            raise RuntimeError('Nexus vault identity mismatch')
        secret = document['secret']
        existing = destination.get(provider_id)
        if existing is not None and existing != secret:
            raise RuntimeError('Destination differs; refusing to overwrite existing key')
        if existing is None:
            destination.set(provider_id, secret)
        imported.append(destination.describe(provider_id))
    store = Store(ROOT)
    with store.connect() as db:
        db.execute("INSERT INTO settings(scope,key,value,updated_at) VALUES('vault','backend','\"dpapi\"',?) "
                   "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (now(),))
    # Reopen verifies ciphertext only, without making a model request.
    restored = keyvault.SecretVault(ROOT, backend='dpapi', directory=ROOT / 'work' / 'secrets')
    assert all(restored.describe(p['provider_id'])['fingerprint'] == p['fingerprint'] for p in imported)
    report = {'migrated': imported, 'reload_verified': True, 'source_unchanged': True,
              'windows_acl_modified': False, 'destination': str(destination.directory)}
    (ROOT / 'outputs' / 'reliability-vault-migration.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'providers_migrated': len(imported), 'reload_verified': True, 'keys_exported': False}))


if __name__ == '__main__':
    main()
