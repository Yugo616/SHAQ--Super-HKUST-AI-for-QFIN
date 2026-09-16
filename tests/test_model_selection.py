import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle import model_backends as backends
import test_research_lab_foundation as foundation


class ModelSelectionTests(unittest.TestCase):
    def test_endpoint_change_cannot_persist_old_key_even_without_probe(self):
        from shaq_daily_oracle.lab_service import LabService, LabServiceError
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            profile = backends.ModelProfile('api', 'openai-chat-completions', 'https://new.example/v1', 'model').public_dict()
            with patch.object(lab, 'connection_secret', return_value=''), patch.object(lab.settings, 'save_model_profile') as save:
                with self.assertRaisesRegex(LabServiceError, 'Key'):
                    lab.save_model_profile(profile, secret='', probe=False)
                save.assert_not_called()

    def test_api_model_switch_reuses_key_only_for_same_connection(self):
        from shaq_daily_oracle.lab_service import LabService
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            old = backends.ModelProfile('api-model', 'openai-chat-completions', 'https://relay.example/v1', 'old-model').public_dict()
            with patch.object(lab.settings, 'load', return_value={'model_profiles':[old]}), patch.object(lab.settings, 'get_model_secret', return_value='stored-key') as read:
                self.assertEqual(lab.connection_secret({**old,'model':'new-model'}, ''), 'stored-key')
                self.assertEqual(lab.connection_secret({**old,'base_url':'https://different.example/v1'}, ''), '')
                self.assertEqual(lab.connection_secret({**old,'protocol':'openai-responses'}, ''), '')
                self.assertEqual(read.call_count, 1)

    def test_old_default_profile_stops_before_collection(self):
        from shaq_daily_oracle.lab_service import LabService
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            profile = backends.ModelProfile('my-codex','codex-cli','','subscription-default')
            with patch.object(lab, '_require_today_available'), patch.object(lab, '_resolve_variants', return_value=[]), patch.object(lab.settings, 'model_profile', return_value=profile), patch('shaq_daily_oracle.lab_service.start_guarded_thread'):
                with self.assertRaisesRegex(backends.ModelBackendError, '选择.*模型'):
                    lab.start_batch(selections=[])

    def test_unresolved_subscription_cannot_invoke_an_expensive_default(self):
        for protocol, call in [('codex-cli', backends._codex_cli_call),
                               ('claude-code', backends._claude_code_call)]:
            profile = backends.ModelProfile('local', protocol, '', 'subscription-default')
            with patch.object(backends, '_local_cli', side_effect=AssertionError('must not launch')):
                with self.assertRaisesRegex(backends.ModelBackendError, '选择.*模型'):
                    call(profile=profile, prompt='test', schema={})

    def test_codex_command_pins_model_and_does_not_claim_requested_as_reported(self):
        profile = backends.ModelProfile('local', 'codex-cli', '', 'chosen-model')
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            Path(command[command.index('--output-last-message') + 1]).write_text('{"status":"ready"}')
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(backends, '_local_cli', return_value='/codex'), patch.object(backends, 'run_model_process', side_effect=run):
            _, audit = backends._codex_cli_call(profile=profile, prompt='test', schema={})
        self.assertEqual(commands[0][commands[0].index('--model') + 1], 'chosen-model')
        self.assertEqual(audit.get('requested_model'), 'chosen-model')
        self.assertFalse(audit['response_model'])

    def test_claude_command_pins_model_and_reads_returned_model_usage(self):
        profile = backends.ModelProfile('local', 'claude-code', '', 'chosen-model')
        def run(command, **kwargs):
            self.assertEqual(command[command.index('--model') + 1], 'chosen-model')
            return subprocess.CompletedProcess(command, 0, json.dumps({
                'structured_output': {'status': 'ready'},
                'modelUsage': {'chosen-model-20260916': {'inputTokens': 5}}
            }), '')
        with patch.object(backends, '_local_cli', return_value='/claude'), patch.object(backends, 'run_model_process', side_effect=run):
            _, audit = backends._claude_code_call(profile=profile, prompt='test', schema={})
        self.assertEqual(audit['response_model'], 'chosen-model-20260916')

    def test_successful_switch_updates_future_schedule_but_not_frozen_profile(self):
        from shaq_daily_oracle.lab_service import LabService
        with tempfile.TemporaryDirectory() as tmp:
            lab = LabService(foundation.ResearchLabFoundationTests().paths(Path(tmp)))
            old = backends.ModelProfile('my-codex', 'codex-cli', '', 'old-model').public_dict()
            new = backends.ModelProfile('my-claude', 'claude-code', '', 'new-model').public_dict()
            with patch('keyring.get_password', return_value=None):
                lab.save_model_profile(old, secret='', probe=False)
                frozen = lab.settings.model_profile().public_dict()
                path = lab.paths.research_root / 'schedule.json'
                path.write_text(json.dumps({'enabled': False, 'start_et': '08:35:00',
                                            'selections': [], 'model_profile_id': 'my-codex'}))
                with patch('shaq_daily_oracle.lab_service.probe_model_profile', side_effect=ValueError('bad model')):
                    with self.assertRaises(ValueError):
                        lab.save_model_profile(new, secret='')
                self.assertEqual(lab.settings.model_profile().model, 'old-model')
                lab.save_model_profile(new, secret='', probe=False)
                schedule = json.loads(path.read_text())
                self.assertEqual(schedule['model_profile_id'], 'my-claude')
                self.assertFalse(schedule['enabled'])
                self.assertEqual(frozen['model'], 'old-model')


class ModelCatalogTests(unittest.TestCase):
    def test_api_catalog_uses_selected_endpoint_and_never_follows_redirect(self):
        from shaq_daily_oracle.model_catalog import api_model_catalog
        profile = backends.ModelProfile('api', 'openai-chat-completions', 'https://relay.example/v1', 'catalog-only')
        import httpx
        captured = {}
        def get(url, **kwargs):
            captured.update(url=url, **kwargs)
            return httpx.Response(200, json={'data': [{'id': 'relay-model'}]}, request=httpx.Request('GET',url))
        with patch('httpx.get', side_effect=get):
            result = api_model_catalog(profile.public_dict(), 'private-key')
        self.assertEqual(captured['url'], 'https://relay.example/v1/models')
        self.assertFalse(captured['follow_redirects'])
        self.assertEqual(result['models'], [{'id':'relay-model', 'label':'relay-model'}])
        self.assertNotIn('private-key', json.dumps(result))

    def test_real_stdio_handshake_requests_metadata_only(self):
        import sys
        from shaq_daily_oracle.model_catalog import _exchange
        fake = '''import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='initialize': print(json.dumps({'id':r['id'],'result':{}}),flush=True)
 elif r['method']=='initialized': pass
 elif r['method']=='model/list': print(json.dumps({'id':r['id'],'result':{'data':[{'model':'metadata-only'}],'nextCursor':None}}),flush=True)
 else: raise RuntimeError('inference forbidden')
'''
        self.assertEqual(_exchange([sys.executable, '-u', '-c', fake], 'codex-cli'), [{'model': 'metadata-only'}])

    def test_real_claude_control_handshake_requests_no_prompt(self):
        import sys
        from shaq_daily_oracle.model_catalog import _exchange
        fake = '''import json,sys
r=json.loads(sys.stdin.readline())
assert r['type']=='control_request' and r['request']['subtype']=='initialize'
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':r['request_id'],'response':{'models':[{'value':'claude-from-cli'}]}}}),flush=True)
sys.stdin.read()
'''
        self.assertEqual(_exchange([sys.executable, '-u', '-c', fake], 'claude-code'), [{'value': 'claude-from-cli'}])

    def test_codex_catalog_uses_provider_names_and_skips_hidden_no_auto_selection(self):
        from shaq_daily_oracle.model_catalog import normalize_catalog
        models = normalize_catalog('codex-cli', [
            {'model': 'future-small', 'displayName': 'Future Small', 'hidden': False},
            {'model': 'hidden-big', 'hidden': True},
            {'model': 'future-small'}, {'model': 'subscription-default'},
        ])
        self.assertEqual(models, [{'id': 'future-small', 'label': 'Future Small'}])

    def test_claude_catalog_reads_cli_models_without_guessed_product_list(self):
        from shaq_daily_oracle.model_catalog import normalize_catalog
        self.assertEqual(normalize_catalog('claude-code', [
            {'value': 'future-claude', 'displayName': 'Future Claude'},
            {'value': 'default', 'displayName': 'Default'},
        ]), [{'id': 'future-claude', 'label': 'Future Claude'}])
