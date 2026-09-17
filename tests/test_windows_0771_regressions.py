import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex
from shaq_daily_oracle.research_settings import ResearchSettingsStore


class WindowsUpgradeRegressions(unittest.TestCase):
    def test_placeholder_models_are_not_setup_ready(self):
        store = object.__new__(ResearchSettingsStore)
        for model in ('default', 'subscription-default', ''):
            with self.subTest(model=model):
                row = dict(profile_id='codex', protocol='codex-cli', model=model,
                           base_url='', auth_style='bearer', output_mode='strict')
                self.assertFalse(store._model_profile_ready({'model_profiles': [row]}, 'codex'))
        row['model'] = 'explicit-model'
        self.assertTrue(store._model_profile_ready({'model_profiles': [row]}, 'codex'))

    def test_rebuild_does_not_hold_write_lock_while_verifying_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / 'batches' / 'LAB-2026-09-16-test'
            batch.mkdir(parents=True)
            (batch / 'batch_manifest.json').write_text(json.dumps({'batch_id': batch.name}))
            index = ResearchDashboardIndex(batches_root=batch.parent, database=root / 'index.db')
            with closing(index._connect()) as con:
                con.execute('CREATE TABLE lock_probe (value INTEGER)')
                con.commit()
            def detail(_):
                # A second connection may update while slow evidence verification runs.
                with closing(sqlite3.connect(index.database, timeout=0.05)) as con:
                    con.execute('INSERT INTO lock_probe VALUES (1)')
                    con.commit()
                return {'evidence': {'candidates': [], 'cutoff_status': 'on_time'}}
            with patch.object(index, 'batch_detail', side_effect=detail):
                index.rebuild()
            with closing(sqlite3.connect(index.database)) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM lock_probe').fetchone()[0], 1)

    def test_sqlite_lock_is_retryable_but_corruption_is_not(self):
        from shaq_daily_oracle.data_retry import failure_diagnostic, is_transient_diagnostic
        self.assertTrue(is_transient_diagnostic(failure_diagnostic(
            sqlite3.OperationalError('database is locked'), 'minute_settlement')))
        self.assertFalse(is_transient_diagnostic(failure_diagnostic(
            sqlite3.DatabaseError('database disk image is malformed'), 'minute_settlement')))
