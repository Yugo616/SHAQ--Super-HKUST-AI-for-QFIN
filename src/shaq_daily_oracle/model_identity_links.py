"""Explicit local model-identity corrections; never inferred from display names."""
import json
import re

from .hashing import sha256_file, sha256_payload


def read_model_links(root):
    links = {}
    for path in sorted((root / 'model_identity_links').glob('*.json')):
        value = json.loads(path.read_text(encoding='utf-8'))
        if path.stem != sha256_payload(value) or value.get('source') != 'user_confirmation':
            raise ValueError('模型归属校正文件校验失败，未合并账户')
        members = value.get('identities', [])
        canonical = value.get('canonical_identity')
        model = value.get('model', '')
        if (not members or len(set(members)) != len(members) or canonical not in members
                or not all(isinstance(item, str) and re.fullmatch(r'[a-f0-9]{64}', item) for item in members)
                or not isinstance(model, str) or not model.strip()
                or model.lower() in {'subscription-default', 'default', 'unknown'}):
            raise ValueError('模型归属校正缺少明确型号或身份，未合并账户')
        covered = set()
        for anchor in value.get('anchors', []):
            relative = anchor.get('path', '')
            file = root / relative
            if (not relative.startswith('batches/') or '\\' in relative
                    or not file.resolve().is_relative_to((root / 'batches').resolve())
                    or file.name != 'variant_result.json' or sha256_file(file) != anchor.get('sha256')):
                raise ValueError('模型归属校正与原始结果不符，未合并账户')
            variant = json.loads(file.read_text(encoding='utf-8'))
            identity = variant.get('model_profile_sha256')
            recorded = variant.get('model_name', '')
            if identity not in members or recorded not in {'', 'subscription-default', 'default', model}:
                raise ValueError('模型归属校正不能覆盖已记录的其他型号')
            covered.add(identity)
        if covered != set(members):
            raise ValueError('模型归属校正缺少原始结果依据')
        for identity in members:
            item = dict(canonical_identity=canonical, model=model, receipt_hash=path.stem)
            if identity in links and links[identity] != item:
                raise ValueError('模型归属校正互相冲突，未合并账户')
            links[identity] = item
    return links
