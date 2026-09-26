import unittest
from unittest.mock import Mock

from shaq_daily_oracle.lab_service import LabService


class ReplayPreloadTests(unittest.TestCase):
    def test_saved_detail_preload_does_not_rebuild_entire_account_overview(self):
        service=object.__new__(LabService)
        service.dashboard=Mock()
        service.dashboard.batch_detail.return_value={'batch_id':'saved'}
        service.dashboard.overview.side_effect=AssertionError('must not rebuild all accounts for each cached detail')
        service.job_statuses=Mock(return_value=[])
        detail=service.batch_detail('saved',include_accounts=False)
        self.assertEqual(detail['batch_id'],'saved')
        self.assertEqual(detail['research_progress'],[])
        self.assertNotIn('virtual_accounts',detail)
