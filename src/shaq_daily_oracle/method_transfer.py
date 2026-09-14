"""Explicit, pinned method transfers; no status-poll or runtime identity changes."""
from __future__ import annotations

import uuid
from typing import Any

from .hashing import sha256_payload
from .skill_versions import SkillVersionError, transfer_content_identity


class MethodTransfer:
    def __init__(self, registry):
        self.registry = registry
        self.operations: dict[str, dict[str, Any]] = {}

    def local_rows(self):
        rows = []
        for row in self.registry.list_versions():
            if row['version_id'] == 'main':
                continue
            value = {**row, 'content_sha256': None, 'error': ''}
            try:
                manifest, files = self.registry.transfer_package(row['version_id'], row['author'])
                value['content_sha256'] = transfer_content_identity(manifest, files)
                value['_package'] = (manifest, files)
            except Exception as exc:
                value['error'] = str(exc)
            rows.append(value)
        return rows

    def catalog(self, client):
        locals_ = self.local_rows()
        local_contents = {r['content_sha256'] for r in locals_ if r['content_sha256']}
        local_manifests = {r['version_sha256'] for r in locals_ if not r['error']}
        grouped = {}
        # Discovery errors propagate. Individual corrupt packages remain visible.
        for row in client.list_remote_versions():
            value = {**row, 'content_sha256': None, 'error': '', 'installed': False}
            try:
                manifest, files, commit = client.download_version(branch=row['branch'],
                    author=row['author'], version_id=row['version_id'], commit_sha=row['commit_sha'])
                if manifest.identity() != row['manifest_sha256']:
                    raise SkillVersionError('remote manifest changed from selected snapshot')
                value['content_sha256'] = transfer_content_identity(manifest, files)
                value['_package'] = (manifest, files, commit)
                value['installed'] = (value['content_sha256'] in local_contents if value['content_sha256']
                    else manifest.identity() in local_manifests)
            except Exception as exc:
                value['error'] = str(exc)
            key = value['content_sha256'] or row['manifest_sha256']
            aliases = row.get('source_aliases') or [{k: row[k] for k in ('author', 'version_id', 'branch', 'commit_sha')}]
            if key in grouped:
                grouped[key]['source_aliases'].extend(aliases)
                continue
            value.update(key=key, source_aliases=list(aliases), eligible=not value['installed'] and not value['error'])
            value['local_status'] = ('本地已有相同内容' if value['installed'] else
                value['error'] or ('可下载' if value['content_sha256'] else '身份不可用：旧包基底未核验'))
            grouped[key] = value
        return list(grouped.values())

    @staticmethod
    def public(row):
        return {k: v for k, v in row.items() if not k.startswith('_')}

    @staticmethod
    def destination_fingerprint(client):
        return sha256_payload({field: getattr(client.config, field) for field in (
            'owner', 'repository', 'catalog_branch', 'skill_package_root', 'api_base_url')})

    def open(self, direction, client, *, login, upload_allowed):
        if direction not in {'upload', 'download'}:
            raise SkillVersionError('unknown transfer direction')
        remote = self.catalog(client)
        rows = remote
        if direction == 'upload':
            remote_contents = {r['content_sha256'] for r in remote if r['content_sha256']}
            remote_error = any(r['error'] for r in remote)
            rows = self.local_rows()
            for row in rows:
                row['key'] = row['version_sha256']
                row['already_remote'] = bool(row['content_sha256'] and row['content_sha256'] in remote_contents)
                row['eligible'] = bool(upload_allowed and login and row['author'] == login.lower()
                    and row['source'] == 'local' and row['content_sha256'] and not row['error']
                    and not row['already_remote'] and not remote_error)
                row['local_status'] = (row['error'] or ('远端清单存在校验失败，请重试' if remote_error else
                    '远端已有相同内容' if row['already_remote'] else
                    '身份不可用：旧包基底未核验' if not row['content_sha256'] else
                    '当前账号只读，不能上传' if not upload_allowed else
                    '非当前账号的本地作品，不可重新署名上传' if row['author'] != login.lower() or row['source'] != 'local' else '可上传'))
                row['source_aliases'] = [a for r in remote if r['content_sha256'] == row['content_sha256'] and row['content_sha256'] for a in r['source_aliases']]
        operation_id = uuid.uuid4().hex
        # Keep the independently open upload/download selections; bound retained
        # pinned packages to one operation per direction.
        self.operations = {k: v for k, v in self.operations.items() if v['direction'] != direction}
        self.operations[operation_id] = {'direction': direction, 'rows': {r['key']: r for r in rows},
            'destination_fingerprint': self.destination_fingerprint(client), 'complete': {}}
        config = client.config
        return {'operation_id': operation_id, 'rows': [self.public(r) for r in rows],
            'destination': f'{config.owner}/{config.repository} · {config.catalog_branch}/{config.skill_package_root}',
            'upload_allowed': upload_allowed,
            'note': '未保存的草稿不在清单内，请先保存为新版本。' if direction == 'upload' else '创建时间不是 GitHub 上传时间；所选内容已固定到清单快照。'}

    def execute(self, operation_id, keys, client, *, login, upload_allowed):
        operation = self.operations.get(operation_id)
        if not operation:
            raise SkillVersionError('传输清单已失效，请刷新后重新选择')
        if operation['direction'] == 'upload' and operation['destination_fingerprint'] != self.destination_fingerprint(client):
            raise SkillVersionError('上传目标已变化，请刷新清单并重新确认目标后选择版本')
        results = []
        for key in dict.fromkeys(keys):
            try:
                if key in operation['complete']:
                    results.append(operation['complete'][key])
                    continue
                row = operation['rows'].get(key)
                if not row or not row['eligible']:
                    raise SkillVersionError('该版本不可传输，请刷新清单')
                if operation['direction'] == 'download':
                    manifest, files, commit = row['_package']
                    transfer_content_identity(manifest, files)
                    current = self.local_rows()
                    if not any((row['content_sha256'] and r['content_sha256'] == row['content_sha256'])
                               or r['version_sha256'] == manifest.identity() for r in current if not r['error']):
                        self.registry.install(manifest=manifest, files=files, commit_sha=commit)
                    result = {'key': key, 'status': 'complete', 'message': '已下载（已核验清单快照）'}
                else:
                    if not upload_allowed or row['author'] != login.lower() or row['source'] != 'local':
                        raise SkillVersionError('当前账号无权上传此版本')
                    manifest, files = self.registry.transfer_package(row['version_id'], row['author'])
                    if manifest.identity() != row['version_sha256'] or transfer_content_identity(manifest, files) != row['content_sha256']:
                        raise SkillVersionError('本地版本已变化，请刷新清单')
                    receipt = client.upload_version(login=login, manifest=manifest, files=files, deduplicate_content=True)
                    result = {'key': key, 'status': 'complete', 'message': '远端已有相同内容' if receipt.get('already_remote') else '已上传',
                        'source_author': receipt['manifest']['author'], 'source_version': receipt['manifest']['version_id']}
                operation['complete'][key] = result
                results.append(result)
            except Exception as exc:
                results.append({'key': key, 'status': 'failed', 'message': str(exc)})
        return results
