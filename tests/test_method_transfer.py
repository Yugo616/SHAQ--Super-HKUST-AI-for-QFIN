import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle import skill_versions as sv
from test_team_version_registry import FakeGitHub

ROOT = Path(__file__).parents[1]


class MethodTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = sv.LocalSkillRegistry(root=Path(self.tmp.name), package_skills=ROOT / 'skills')
        self.files = json.loads((ROOT / 'bundled_versions/independent-gate.json').read_text())['files']
        self.remote = FakeGitHub()
        self.client = sv.GitHubSkillClient(config=sv.GitHubRepositoryConfig(owner='team', repository='repo', client_id='client'), token='test')
        self.client._request = self.remote.request

    def package(self, author='alice', version='one', files=None):
        return sv.SkillVersionManifest.create(author=author, version_id=version, files=files or self.files,
            base_main_sha='a'*40, description='Saved method')

    def transfer(self):
        from shaq_daily_oracle.method_transfer import MethodTransfer
        return MethodTransfer(self.registry)

    def test_content_identity_ignores_envelope_but_includes_every_file(self):
        self.assertTrue(callable(getattr(sv, 'transfer_content_identity', None)), 'full content identity missing')
        digest = sv.transfer_content_identity(self.package(), self.files)
        self.assertEqual(digest, sv.transfer_content_identity(self.package('bob', 'renamed'), dict(reversed(list(self.files.items())))))
        hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in sorted(self.files.items())}
        expected = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(digest, expected)
        for path in ['skills/market-common-shock/agents/openai.yaml', 'modules/market/compute.js', 'decision/cases.json']:
            content = self.files[path]
            if path.endswith('.yaml'):
                profile = sv.parse_agent_profile(content)
                profile['display_name'] += ' changed'
                content = sv.render_agent_profile(profile)
            else:
                content += '\n'
            changed = {**self.files, path: content}
            self.assertNotEqual(digest, sv.transfer_content_identity(self.package(files=changed), changed))
        with self.assertRaisesRegex(sv.SkillVersionError, 'hash|bytes'):
            sv.transfer_content_identity(self.package(), changed)
        partial = {'skills/market-common-shock/SKILL.md': self.files['skills/market-common-shock/SKILL.md']}
        self.assertIsNone(sv.transfer_content_identity(self.package(files=partial), partial))

    def test_catalog_dedups_names_authors_branches_and_ignores_head_advance(self):
        package = self.package()
        self.registry.install(manifest=package, files=self.files, commit_sha='b'*40)
        self.remote.seed('versions', package, self.files)
        self.remote.seed('shadow/bob', self.package('bob', 'renamed'), self.files)
        self.remote.seed('shadow/alice', package, self.files)
        transfer = self.transfer()
        rows = transfer.catalog(self.client)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['installed'])
        self.assertEqual(len(rows[0]['source_aliases']), 3)
        self.remote.seed('versions', self.package('carl', 'copy'), self.files)
        again = transfer.catalog(self.client)
        self.assertEqual(len(again), 1)
        self.assertTrue(again[0]['installed'])
        self.assertEqual(self.registry.list_versions()[1]['version_sha256'], package.identity())

    def test_upload_saved_only_and_never_reattributes_other_author(self):
        own = self.package()
        other = self.package('bob', 'other')
        self.registry.install(manifest=own, files=self.files, commit_sha='local:'+own.identity())
        self.registry.install(manifest=other, files=self.files, commit_sha='c'*40)
        self.registry.save_draft(author='alice', draft_id='unsaved', files=self.files, base_main_sha='a'*40)
        value = self.transfer().open('upload', self.client, login='alice', upload_allowed=True)
        self.assertEqual({r['version_id'] for r in value['rows']}, {'one', 'other'})
        self.assertTrue(next(r for r in value['rows'] if r['author']=='alice')['eligible'])
        self.assertFalse(next(r for r in value['rows'] if r['author']=='bob')['eligible'])
        self.assertEqual(self.remote.refs, {'main': 'main-sha'})

    def test_concurrent_same_content_upload_does_not_publish_duplicate(self):
        package = self.package()
        duplicate = self.package('bob', 'other')
        self.remote.before_patch = lambda: self.remote.seed('versions', duplicate, self.files)
        result = self.client.upload_version(login='alice', manifest=package, files=self.files, deduplicate_content=True)
        self.assertTrue(result['already_remote'])
        self.assertEqual([(r['author'],r['version_id']) for r in self.client.list_remote_versions()], [('bob','other')])

    def test_download_pins_listed_bytes_and_partial_failure_retains_success(self):
        transfer = self.transfer()
        self.remote.seed('versions', self.package(), self.files)
        value = transfer.open('download', self.client, login='', upload_allowed=False)
        changed = {**self.files, 'modules/market/compute.js': self.files['modules/market/compute.js']+'\n'}
        self.remote.seed('versions', self.package(files=changed), changed)
        # Real git SHAs are forty hex digits; translate only fake commit at install boundary.
        original = self.registry.install
        self.registry.install = lambda **kw: original(**{**kw, 'commit_sha':'d'*40})
        results = transfer.execute(value['operation_id'], [value['rows'][0]['key'], 'invalid'], self.client, login='', upload_allowed=False)
        self.assertEqual([r['status'] for r in results], ['complete','failed'])
        self.assertEqual(self.registry.effective_skills('one','alice')['modules/market/compute.js'], self.files['modules/market/compute.js'])
        retry = transfer.execute(value['operation_id'], [value['rows'][0]['key']], self.client, login='', upload_allowed=False)
        self.assertEqual(retry[0]['status'], 'complete')

    def test_invalid_remote_item_is_failure_not_empty_and_readonly_upload_rejected(self):
        self.remote.seed('versions', self.package(), self.files)
        tree = self.remote.trees[self.remote.commits[self.remote.refs['versions']]['tree']]
        tree['shadow_versions/alice/one/changed_modules/market/compute.js'] += b'forged'
        value = self.transfer().open('download', self.client, login='', upload_allowed=False)
        self.assertEqual(len(value['rows']), 1)
        self.assertFalse(value['rows'][0]['eligible'])
        self.assertIn('manifest', value['rows'][0]['error'])
        package = self.package()
        self.registry.install(manifest=package, files=self.files, commit_sha='local:'+package.identity())
        value = self.transfer().open('upload', self.client, login='alice', upload_allowed=False)
        self.assertFalse(value['rows'][0]['eligible'])

    def test_manifest_lookup_401_cannot_be_reported_as_empty_catalog(self):
        self.remote.seed('versions', self.package(), self.files)
        original = self.client.get_content
        def forbidden(path, *, ref):
            if path.endswith('manifest.json'):
                raise sv.SkillVersionError('GitHub request failed: 401')
            return original(path, ref=ref)
        self.client.get_content = forbidden
        with self.assertRaisesRegex(sv.SkillVersionError, '401'):
            self.transfer().catalog(self.client)

    def test_copy_main_and_save_freezes_all_module_defaults_and_keeps_original_authorship(self):
        from shaq_daily_oracle.lab_service import LabService
        lab = object.__new__(LabService)
        lab.registry = self.registry
        lab._local_author = lambda: 'alice'
        copy = lab.copy_local_version(version_id='main', author='team')
        saved = lab.finalize_local_version(draft_id=copy['draft_id'], description='new local method')
        manifest, files = self.registry.transfer_package(saved['version_id'], saved['author'])
        self.assertEqual(set(files), sv.COMPLETE_METHOD_PATHS)
        self.assertIsNotNone(sv.transfer_content_identity(manifest, files))
        original_files = {**self.files, 'modules/market/compute.js': self.files['modules/market/compute.js']+'\n'}
        other = self.package('bob', 'original', original_files)
        self.registry.install(manifest=other, files=original_files, commit_sha='c'*40)
        copy = lab.copy_local_version(version_id='original', author='bob')
        saved = lab.finalize_local_version(draft_id=copy['draft_id'], description='rename only')
        self.assertEqual((saved['author'], saved['version_id']), ('bob', 'original'))

    def test_local_collision_keeps_old_bytes_and_unresolved_legacy_is_explicit(self):
        partial = {'skills/market-common-shock/SKILL.md': self.files['skills/market-common-shock/SKILL.md']}
        old = self.package(files=partial)
        target=self.registry.install(manifest=old, files=partial, commit_sha='b'*40)
        before={p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()}
        self.remote.seed('versions', self.package(), self.files)
        transfer=self.transfer()
        value=transfer.open('download',self.client,login='',upload_allowed=False)
        install=self.registry.install
        self.registry.install=lambda **kw:install(**{**kw,'commit_sha':'d'*40})
        result=transfer.execute(value['operation_id'],[value['rows'][0]['key']],self.client,login='',upload_allowed=False)
        self.assertEqual(result[0]['status'],'failed')
        self.assertIn('immutable',result[0]['message'])
        after={p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()}
        self.assertEqual(after,before)
        upload=transfer.open('upload',self.client,login='alice',upload_allowed=True)
        self.assertIn('基底未核验',upload['rows'][0]['local_status'])
        self.assertFalse(upload['rows'][0]['eligible'])

    def test_readonly_recheck_at_confirmation_and_forged_local_file_fail_closed(self):
        package=self.package()
        target=self.registry.install(manifest=package,files=self.files,commit_sha='local:'+package.identity())
        transfer=self.transfer()
        opened=transfer.open('upload',self.client,login='alice',upload_allowed=True)
        failed=transfer.execute(opened['operation_id'],[opened['rows'][0]['key']],self.client,login='alice',upload_allowed=False)
        self.assertEqual(failed[0]['status'],'failed')
        self.assertEqual(self.remote.refs,{'main':'main-sha'})
        (target/'changed_modules/market/compute.js').write_bytes(b'forged')
        failed=transfer.execute(opened['operation_id'],[opened['rows'][0]['key']],self.client,login='alice',upload_allowed=True)
        self.assertIn('hash',failed[0]['message'])
        self.assertEqual(self.remote.refs,{'main':'main-sha'})

    def test_two_saved_uploads_keep_success_when_network_fails_then_retry_remaining(self):
        changed={**self.files,'modules/market/compute.js':self.files['modules/market/compute.js']+'\n'}
        for version,files in [('one',self.files),('two',changed)]:
            package=self.package(version=version,files=files)
            self.registry.install(manifest=package,files=files,commit_sha='local:'+package.identity())
        transfer=self.transfer()
        opened=transfer.open('upload',self.client,login='alice',upload_allowed=True)
        keys=[r['key'] for r in opened['rows']]
        original=self.client._request
        failing=True
        def network(method,path,**kwargs):
            if failing and method=='POST' and path.endswith('/git/commits') and 'two' in kwargs['json']['message']:
                raise sv.SkillVersionError('network unavailable')
            return original(method,path,**kwargs)
        self.client._request=network
        result=transfer.execute(opened['operation_id'],keys,self.client,login='alice',upload_allowed=True)
        self.assertEqual([r['status'] for r in result],['complete','failed'])
        self.assertEqual(len(self.client.list_remote_versions()),1)
        failing=False
        result=transfer.execute(opened['operation_id'],keys,self.client,login='alice',upload_allowed=True)
        self.assertEqual([r['status'] for r in result],['complete','complete'])
        self.assertEqual(len(self.client.list_remote_versions()),2)

    def test_transfer_discovery_preserves_run_and_account_identity(self):
        from shaq_daily_oracle.research_batch import VariantSelection
        package=self.package()
        target=self.registry.install(manifest=package,files=self.files,commit_sha='local:'+package.identity())
        before=(target/'manifest.json').read_bytes()
        row=self.registry.list_versions()[1]
        identity=VariantSelection.from_registry_row(row).identity()
        self.remote.seed('versions',self.package('bob','renamed'),self.files)
        transfer=self.transfer()
        transfer.open('upload',self.client,login='alice',upload_allowed=True)
        transfer.open('download',self.client,login='alice',upload_allowed=True)
        self.assertEqual((target/'manifest.json').read_bytes(),before)
        self.assertEqual(VariantSelection.from_registry_row(self.registry.list_versions()[1]).identity(),identity)

    def test_accepted_patch_lost_response_retry_verifies_equivalence_without_more_writes(self):
        package=self.package()
        self.registry.install(manifest=package,files=self.files,commit_sha='local:'+package.identity())
        transfer=self.transfer()
        opened=transfer.open('upload',self.client,login='alice',upload_allowed=True)
        original=self.client._request
        writes=[]
        lose_response=True
        def accepted_then_lost(method,path,**kwargs):
            nonlocal lose_response
            if method!='GET':writes.append((method,path))
            result=original(method,path,**kwargs)
            if method=='PATCH' and lose_response:
                lose_response=False
                raise sv.SkillVersionError('network lost accepted PATCH response')
            return result
        self.client._request=accepted_then_lost
        keys=[opened['rows'][0]['key']]
        first=transfer.execute(opened['operation_id'],keys,self.client,login='alice',upload_allowed=True)
        self.assertEqual(first[0]['status'],'failed')
        self.assertEqual(len(self.client.list_remote_versions()),1)
        writes_before_retry=len(writes)
        second=transfer.execute(opened['operation_id'],keys,self.client,login='alice',upload_allowed=True)
        self.assertEqual(second[0]['status'],'complete',second)
        self.assertEqual(second[0]['message'],'远端已有相同内容')
        self.assertEqual(len(writes),writes_before_retry)
        self.assertEqual(len(self.client.list_remote_versions()),1)

    def test_same_path_different_verified_content_remains_a_hard_collision(self):
        changed={**self.files,'modules/market/compute.js':self.files['modules/market/compute.js']+'\n'}
        self.remote.seed('versions',self.package(files=changed),changed)
        head=self.remote.refs['versions']
        with self.assertRaisesRegex(sv.SkillVersionError,'already exists'):
            self.client.upload_version(login='alice',manifest=self.package(),files=self.files,deduplicate_content=True)
        self.assertEqual(self.remote.refs['versions'],head)

    def test_upload_confirmation_rejects_changed_destination_before_any_request(self):
        from dataclasses import replace
        package=self.package()
        self.registry.install(manifest=package,files=self.files,commit_sha='local:'+package.identity())
        for field,value in [('owner','other'),('repository','other'),('catalog_branch','other'),
                            ('skill_package_root','other'),('api_base_url','https://other.invalid')]:
            with self.subTest(field=field):
                transfer=self.transfer()
                opened=transfer.open('upload',self.client,login='alice',upload_allowed=True)
                altered=sv.GitHubSkillClient(config=self.client.config,token='test')
                # The client normally also rejects non-governed roots. Exercise
                # the adapter boundary independently so it stays pinned even if
                # a different validated client configuration is later supported.
                altered.config=replace(self.client.config,**{field:value})
                requests=[]
                def forbidden(*args,**kwargs):
                    requests.append(args)
                    raise AssertionError('changed destination must not receive a request')
                altered._request=forbidden
                with self.assertRaisesRegex(sv.SkillVersionError,'目标.*变化|destination'):
                    transfer.execute(opened['operation_id'],[opened['rows'][0]['key']],altered,login='alice',upload_allowed=True)
                self.assertEqual(requests,[])
                self.assertEqual(self.remote.refs,{'main':'main-sha'})
