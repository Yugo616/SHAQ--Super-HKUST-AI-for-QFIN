import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.model_backends import ModelProfile, ModelBackendError, call_structured


class CodexContextLimitTests(unittest.TestCase):
    def test_default_cli_limit_uses_exact_models_advertised_capacity_without_changing_input(self):
        profile = ModelProfile(profile_id='test', protocol='codex-cli', base_url='', model='fixture-model')
        prompt = 'a' * 480000
        with tempfile.TemporaryDirectory() as root, patch('pathlib.Path.home', return_value=Path(root)):
            Path(root, '.codex').mkdir()
            Path(root, '.codex/models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'fixture-model', 'context_window': 272000, 'effective_context_window_percent': 95}]}))
            def backend(**kwargs):
                self.assertEqual(kwargs['prompt'], prompt)
                self.assertEqual(kwargs['profile'], profile)
                return {'status': 'ready'}, {'response_model': 'fixture-model'}
            with patch('shaq_daily_oracle.model_backends._codex_cli_call', backend):
                result, audit = call_structured(profile=profile, secret='', prompt=prompt, schema={'type':'object'})
            self.assertEqual(result, {'status':'ready'})
            self.assertEqual(audit['context_preflight']['effective_limit'], 258400)
            self.assertEqual(audit['profile_sha256'], profile.identity())

    def test_unknown_model_custom_limit_and_api_do_not_inherit_another_capacity(self):
        base = ModelProfile(profile_id='test', protocol='codex-cli', base_url='', model='fixture-model')
        with tempfile.TemporaryDirectory() as root, patch('pathlib.Path.home', return_value=Path(root)):
            Path(root, '.codex').mkdir()
            Path(root, '.codex/models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'fixture-model', 'context_window': 272000, 'effective_context_window_percent': 95}]}))
            for profile in (replace(base, model='unknown'), replace(base, maximum_context_tokens=100000),
                            replace(base, protocol='openai-chat-completions', base_url='https://example.org/v1')):
                with self.subTest(profile=profile), self.assertRaises(ModelBackendError):
                    call_structured(profile=profile, secret='', prompt='a'*480000, schema={'type':'object'})
