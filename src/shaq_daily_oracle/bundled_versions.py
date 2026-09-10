"""Install shipped method packages without replacing personal versions or runs."""
import json
from .skill_versions import SkillVersionManifest, SkillVersionError


def install_bundled_versions(registry):
    for path in sorted((registry.package_skills.parent / 'bundled_versions').glob('*.json')):
        package = json.loads(path.read_text(encoding='utf-8'))
        manifest = SkillVersionManifest.from_dict(package['manifest'])
        target = registry.root / manifest.author / manifest.version_id / 'manifest.json'
        if target.exists():
            stored = json.loads(target.read_text(encoding='utf-8'))
            if stored.get('manifest_sha256') != manifest.identity():
                raise SkillVersionError('内置版本与已安装同名版本不同；请保留原版本并检查安装来源')
            registry.effective_skills(manifest.version_id, manifest.author)
            continue
        registry.install(manifest=manifest, files=package['files'], commit_sha='local:' + manifest.identity())
