"""Verify previously accepted artifacts; never rebuild or modify installers."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

source = '84dc32c4901ccadab53372ee526186d44786d83c'
out = Path('release')
out.mkdir()
reports = []
method_sets = []
with tarfile.open(out / 'SHAQ-macOS-Third-Party-Sources.tar.gz', 'w:gz') as archive:
    for platform, architecture in [('Apple-Silicon', 'arm64'), ('Intel', 'x86_64')]:
        root = Path('accepted') / ('SHAQ-Daily-Oracle-Lab-macOS-' + platform)
        meta = json.loads((root / 'build/third-party/manifest.json').read_text())
        assert meta['source_sha'] == source and not meta['source_dirty']
        assert meta['architecture'] == architecture
        method_sets.append(meta['methods'])
        smoke = json.loads((root / 'dist/installed-smoke.json').read_text())
        audit = json.loads((root / 'dist/installed-native-audit.json').read_text())
        gui = json.loads((root / 'dist/installed-gui.json').read_text())
        assert smoke['status'] == 'passed' and smoke['checks'] and all(smoke['checks'].values())
        assert audit['status'] == 'passed' and not audit['failures'] and audit['native_count'] > 0
        assert gui['status'] == 'passed' and set(gui['pages']) == {'run', 'editor', 'history'}
        dmg = root / 'dist' / ('SHAQ-Daily-Oracle-Lab-macOS-' + platform + '.dmg')
        sidecar = Path(str(dmg) + '.sha256')
        digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
        assert digest == sidecar.read_text().split()[0]
        shutil.copy2(dmg, out / dmg.name)
        shutil.copy2(sidecar, out / sidecar.name)
        reports.append({'platform': platform, 'source_sha': source, 'sha256': digest,
                        'installed_checks': smoke['checks'], 'native_count': audit['native_count'],
                        'pages': gui['pages'], 'status': 'passed', 'model_calls': 'fixed fixtures only'})
        for name in ['build/third-party', 'build/license-sources', 'build/native-dependencies/sources',
                     'build/native-dependencies/wheel-sha256.json']:
            archive.add(root / name, arcname=platform + '/' + name)
assert method_sets[0] == method_sets[1]
(out / 'macOS-Acceptance.json').write_text(json.dumps({'status': 'passed', 'run_id': 34486634455,
    'source_sha': source, 'artifacts': reports}, indent=2), encoding='utf-8')
