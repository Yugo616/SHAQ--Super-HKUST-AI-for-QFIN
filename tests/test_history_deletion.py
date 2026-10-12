import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shaq_daily_oracle.research_dashboard import ResearchDashboardIndex, ResearchDashboardError


class HistoryDeletionTests(unittest.TestCase):
    def test_delete_only_superseded_record_without_touching_shared_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = ResearchDashboardIndex(batches_root=root/'batches', database=root/'index.db')
            row = dict(batch_id='old', variant_key='team/main', variant_result_sha256='digest',
                       can_delete=True, superseded_by='new')
            evidence = root/'shared-evidence'; evidence.write_text('unchanged')
            with patch.object(index, 'overview', return_value={'daily_results':[row]}):
                result = index.delete_history_result('old', 'team/main', 'digest')
            self.assertEqual(result['status'], 'deleted')
            self.assertTrue(index._history_deleted(row))
            self.assertFalse(index._history_deleted(dict(row, variant_result_sha256='changed')))
            self.assertEqual(evidence.read_text(), 'unchanged')
            with patch.object(index, 'overview', return_value={'daily_results':[dict(row,can_delete=False)]}):
                with self.assertRaises(ResearchDashboardError):
                    index.delete_history_result('old', 'team/main', 'digest')
            with patch.object(index, 'overview', return_value={'daily_results':[row]}):
                with self.assertRaises(ResearchDashboardError):
                    index.delete_history_result('old', 'team/main', 'wrong')

    def test_button_and_confirmation_are_scoped_to_obsolete_record(self):
        source=(Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/review.js').read_text()
        self.assertIn('data-delete-history',source)
        self.assertIn("api('delete_history_result'",source)
        self.assertIn('confirm(',source)
        self.assertIn('row.can_delete',source)

if __name__ == '__main__':
    unittest.main()
