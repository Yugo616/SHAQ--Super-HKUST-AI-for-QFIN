import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_research_batch as fixtures
from shaq_daily_oracle.research_batch import ResearchBatchRunner, load_frozen_evidence, ResearchBatchError
from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex, ResearchDashboardError


class VerifiedDetailCacheTests(unittest.TestCase):
    def make_batch(self, root):
        fixture = fixtures.ResearchBatchTests()
        registry = fixture.registry(root)
        staged = fixture.evidence(root/'staged')
        target = root/'evidence'/staged.manifest['evidence_hash']
        target.parent.mkdir(parents=True)
        staged.root.replace(target)
        result = ResearchBatchRunner(batches_root=root/'batches', cache_root=root/'cache',
            registry=registry, integration_policy=fixture.policy()).run(
            evidence=load_frozen_evidence(target), variants=[fixture.main_variant(registry)],
            profile=fixture.profile(), secret='test', caller=fixtures.FakeModel())
        return result['status']['batch_id'], target

    def test_verified_history_is_reused_without_rehashing_unchanged_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); batch,_=self.make_batch(root)
            view=ResearchDashboardIndex(batches_root=root/'batches', database=root/'index.sqlite3')
            with patch('shaq_daily_oracle.research_dashboard.load_frozen_evidence', wraps=load_frozen_evidence) as read:
                first=view.batch_detail(batch)
                first['variants'].clear()  # Caller edits cannot poison the verified cache.
                second=view.batch_detail(batch)
                view.overview()
                self.assertTrue(second['variants'])
                self.assertEqual(read.call_count,1)

    def test_same_size_tamper_with_restored_mtime_invalidates_cached_verification(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); batch,evidence=self.make_batch(root)
            view=ResearchDashboardIndex(batches_root=root/'batches',database=root/'index.sqlite3')
            view.batch_detail(batch)
            file=evidence/'raw/market.json'; stat=file.stat()
            file.write_bytes(file.read_bytes().replace(b'0.01',b'0.02'))
            os.utime(file,ns=(stat.st_atime_ns,stat.st_mtime_ns))
            with self.assertRaises((ResearchBatchError,ResearchDashboardError)): view.batch_detail(batch)

    def test_label_creation_refreshes_cached_detail(self):
        from shaq_daily_oracle.hashing import sha256_payload
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); batch,_=self.make_batch(root)
            view=ResearchDashboardIndex(batches_root=root/'batches',database=root/'index.sqlite3')
            self.assertEqual(view.batch_detail(batch)['labels'],{'labels':{}})
            value={'labels':{'AAPL':{'status':'provisional','actual_direction':'bullish'}}}
            (root/'batches'/batch/'labels.json').write_text(json.dumps(dict(value,labels_sha256=sha256_payload(value))))
            self.assertEqual(view.batch_detail(batch)['labels']['labels']['AAPL']['status'],'provisional')
